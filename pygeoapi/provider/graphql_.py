# =================================================================
#
# Authors: Luis Calisto <luis.calisto@calisto.pt>
#
# Copyright (c) 2026 Luis Calisto
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

import functools
import logging
import math
import re
from urllib.parse import urlparse

import requests
from graphql import (GraphQLError, build_client_schema,
                     get_introspection_query, get_named_type, is_enum_type,
                     is_input_object_type, is_list_type, is_non_null_type,
                     is_object_type, is_required_argument, is_scalar_type)

from pygeoapi.crs import crs_transform
from pygeoapi.provider.base import (BaseProvider, ProviderConnectionError,
                                    ProviderGenericError,
                                    ProviderInvalidQueryError,
                                    ProviderQueryError)

LOGGER = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT = 30

INT32_MAX = 2 ** 31 - 1

GEOJSON_TYPES = {'Point', 'MultiPoint', 'LineString', 'MultiLineString',
                 'Polygon', 'MultiPolygon', 'GeometryCollection'}

GRAPHQL_NAME = re.compile(r'[_A-Za-z][_0-9A-Za-z]*')

GRAPHQL_KEYS = {'collection', 'geometry', 'bbox_filter', 'page_size',
                'field_mapping', 'headers', 'timeout'}

#: GraphQL scalar name to JSON Schema; anything else is a string
TYPE_MAP = {
    'Int': {'type': 'integer'},
    'Float': {'type': 'number'},
    'Boolean': {'type': 'boolean'},
    'Date': {'type': 'string', 'format': 'date'},
    'Datetime': {'type': 'string', 'format': 'date-time'},
    'JSON': {'type': 'object'},
    'GeoJSON': {'type': 'object'},
}


def _config_error(msg):
    """
    Log and raise a configuration error

    :param msg: error text
    """

    msg = f'GraphQL provider configuration: {msg}'
    LOGGER.error(msg)
    raise ProviderGenericError(msg)


def _valid_name(value, key, dotted=False):
    """
    Validate a configured GraphQL name

    :param value: configured value
    :param key: configuration key, for the error message
    :param dotted: whether a dotted path is allowed

    :returns: the value
    """

    if not isinstance(value, str) or not value:
        _config_error(f'{key} must be a GraphQL name')

    parts = value.split('.') if dotted else [value]
    if not all(GRAPHQL_NAME.fullmatch(part) for part in parts):
        _config_error(f'{key}: invalid name {value!r}')

    return value


def _positive_int(value, key):
    """
    Validate a configured positive integer

    :param value: configured value
    :param key: configuration key, for the error message

    :returns: the value
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _config_error(f'{key} must be a positive integer')

    return value


def _post_graphql(session, url, headers, timeout, document, variables=None):
    """
    POST a GraphQL document, the only call site in this module

    :param session: requests.Session
    :param url: endpoint URL
    :param headers: dict of HTTP headers
    :param timeout: request timeout in seconds
    :param document: GraphQL document
    :param variables: dict of variables

    :returns: dict of GraphQL reply data
    """

    payload = {'query': document}
    if variables:
        payload['variables'] = variables

    try:
        response = session.post(url, json=payload, headers=headers,
                                timeout=timeout, allow_redirects=False)
    except requests.RequestException as err:
        msg = f'Transport error: {err}'
        LOGGER.error(msg)
        raise ProviderConnectionError(msg)

    if 300 <= response.status_code < 400:
        msg = f'Unexpected redirect from {url}'
        LOGGER.error(msg)
        raise ProviderConnectionError(msg)

    try:
        body = response.json()
    except ValueError:
        msg = f'Non-JSON reply from {url}'
        LOGGER.error(msg)
        raise ProviderConnectionError(msg)

    if isinstance(body, dict) and body.get('errors'):
        msg = f"GraphQL errors from {url}: {body['errors']}"
        LOGGER.error(msg)
        raise ProviderQueryError(msg)

    if response.status_code != 200:
        msg = f'Status {response.status_code} from {url}'
        LOGGER.error(msg)
        raise ProviderConnectionError(msg)

    data = body.get('data') if isinstance(body, dict) else None
    if not isinstance(data, dict):
        msg = f'Unexpected reply shape from {url}'
        LOGGER.error(msg)
        raise ProviderQueryError(msg)

    return data


@functools.cache
def _schema(url, headers_items, timeout):
    """
    Introspect a GraphQL endpoint, cached per worker

    :param url: endpoint URL
    :param headers_items: tuple of sorted HTTP header items
    :param timeout: request timeout in seconds

    :returns: graphql.GraphQLSchema
    """

    document = get_introspection_query(descriptions=False)
    LOGGER.debug(f'Introspecting {url}')
    with requests.Session() as session:
        data = _post_graphql(session, url, dict(headers_items), timeout,
                             document)

    try:
        return build_client_schema(data)
    except (TypeError, KeyError, GraphQLError) as err:
        msg = f'Invalid introspection reply from {url}: {err}'
        LOGGER.error(msg)
        raise ProviderQueryError(msg)


def _field_schema(type_):
    """
    JSON Schema fragment for a GraphQL field type

    :param type_: GraphQL field type

    :returns: dict of JSON Schema keys
    """

    if is_non_null_type(type_):
        type_ = type_.of_type
    if is_list_type(type_):
        return {'type': 'array'}

    named = get_named_type(type_)
    return dict(TYPE_MAP.get(named.name, {'type': 'string'}))


def _order_map(mapping, order_type):
    """
    Map property names to orderBy enum value pairs by normalised name;
    keys come from the mapped GraphQL field, dotted paths are not sortable

    :param mapping: property mapping
    :param order_type: orderBy GraphQL enum type

    :returns: dict of property name to (asc, desc) enum values
    """

    asc = {}
    desc = {}
    ambiguous = set()
    for name in order_type.values:
        if name.endswith('_ASC'):
            key = name[:-4].replace('_', '').lower()
            if key in asc:
                ambiguous.add(key)
            asc[key] = name
        elif name.endswith('_DESC'):
            key = name[:-5].replace('_', '').lower()
            if key in desc:
                ambiguous.add(key)
            desc[key] = name

    pairs = {}
    for prop, (path, _) in mapping.items():
        if len(path) != 1:
            continue
        key = path[0].replace('_', '').lower()
        if key in ambiguous:
            continue
        if key in asc and key in desc:
            pairs[prop] = (asc[key], desc[key])

    return pairs


def _render_tree(tree, indent):
    """
    Render a selection tree as GraphQL lines

    :param tree: nested dict of field names
    :param indent: leading whitespace

    :returns: list of lines
    """

    lines = []
    for name, sub in tree.items():
        if sub:
            lines.append(f'{indent}{name} {{')
            lines += _render_tree(sub, indent + '  ')
            lines.append(f'{indent}}}')
        else:
            lines.append(f'{indent}{name}')

    return lines


def _unsupported(msg):
    """
    Log and raise an unsupported-parameter error

    :param msg: error text, also sent to the client
    """

    LOGGER.error(msg)
    raise ProviderInvalidQueryError(user_msg=msg)


def _value_at(node, path):
    """
    Value at a field path inside a reply node

    :param node: reply node
    :param path: field path tuple

    :returns: the value, or None
    """

    value = node
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)

    return value


class GraphQLProvider(BaseProvider):
    """GraphQL provider"""

    def __init__(self, provider_def):
        """
        Initialize object

        :param provider_def: provider definition

        :returns: pygeoapi.provider.graphql_.GraphQLProvider
        """

        super().__init__(provider_def)
        self._parse_config(provider_def)
        self._session = requests.Session()
        self._resolve_schema()
        self.get_fields()

    def get_fields(self):
        """
        Get provider field information (names, types)

        :returns: dict of fields
        """

        self._fields = {prop: _field_schema(type_)
                        for prop, (_, type_) in self._mapping.items()}
        return self._fields

    def _parse_config(self, provider_def):
        """
        Validate the static provider configuration

        :param provider_def: provider definition
        """

        if not isinstance(self.data, str):
            _config_error('data must be an http(s) URL')
        url = urlparse(self.data)
        if url.scheme not in ('http', 'https') or not url.netloc:
            _config_error('data must be an http(s) URL')

        if not self.id_field:
            _config_error('id_field is required')

        if self.editable:
            _config_error('editable is not supported (read only)')

        if self.time_field is not None:
            _config_error('time_field is not supported')

        graphql = provider_def.get('graphql')
        if graphql is None:
            _config_error('graphql block is required')
        if not isinstance(graphql, dict):
            _config_error('graphql block must be a mapping')

        unknown = sorted(map(str, set(graphql) - GRAPHQL_KEYS))
        if unknown:
            _config_error(f"unknown keys: {', '.join(unknown)}")

        if 'collection' not in graphql:
            _config_error('collection is required')

        self.collection = _valid_name(graphql['collection'], 'collection')
        self._parse_geometry(graphql.get('geometry'))
        self._parse_bbox_filter(graphql.get('bbox_filter'))
        self._parse_field_mapping(graphql.get('field_mapping'))
        self._parse_headers(graphql.get('headers'))
        self.page_size = _positive_int(
            graphql.get('page_size', DEFAULT_PAGE_SIZE), 'page_size')
        self.timeout = _positive_int(
            graphql.get('timeout', DEFAULT_TIMEOUT), 'timeout')

    def _parse_geometry(self, geometry):
        """
        Validate the graphql.geometry block

        :param geometry: geometry configuration
        """

        self.geometry_x = None
        self.geometry_y = None
        self.geometry_geojson = None

        if geometry is None:
            return
        if not isinstance(geometry, dict):
            _config_error('geometry must be a mapping')

        keys = set(geometry)
        if keys == {'x_field', 'y_field'}:
            self.geometry_x = _valid_name(
                geometry['x_field'], 'geometry.x_field', dotted=True)
            self.geometry_y = _valid_name(
                geometry['y_field'], 'geometry.y_field', dotted=True)
        elif keys == {'geojson_field'}:
            self.geometry_geojson = _valid_name(
                geometry['geojson_field'], 'geometry.geojson_field',
                dotted=True)
        else:
            _config_error(
                'geometry needs x_field and y_field, or geojson_field')

    def _parse_bbox_filter(self, bbox_filter):
        """
        Validate the graphql.bbox_filter block

        :param bbox_filter: bbox_filter configuration
        """

        self.bbox_field = None
        self.bbox_operator = None

        if bbox_filter is None:
            return
        if (not isinstance(bbox_filter, dict)
                or set(bbox_filter) != {'field', 'operator'}):
            _config_error('bbox_filter needs field and operator')

        self.bbox_field = _valid_name(
            bbox_filter['field'], 'bbox_filter.field')
        self.bbox_operator = _valid_name(
            bbox_filter['operator'], 'bbox_filter.operator')

    def _parse_field_mapping(self, mapping):
        """
        Validate the graphql.field_mapping block

        :param mapping: field_mapping configuration
        """

        self.field_mapping = {}

        if mapping is None:
            return
        if not isinstance(mapping, dict) or not mapping:
            _config_error('field_mapping must be a non-empty mapping')

        for prop, path in mapping.items():
            if not isinstance(prop, str) or not prop:
                _config_error('field_mapping keys must be property names')
            self.field_mapping[prop] = _valid_name(
                path, f'field_mapping.{prop}', dotted=True)

    def _parse_headers(self, headers):
        """
        Validate the graphql.headers block; values are never logged

        :param headers: headers configuration
        """

        self.headers = {}

        if headers is None:
            return
        if not isinstance(headers, dict):
            _config_error('headers must be a mapping')

        for key, value in headers.items():
            if (not isinstance(key, str) or value is None
                    or isinstance(value, (dict, list))):
                _config_error('headers must map names to scalar values')
            self.headers[key] = str(value)

    def _resolve_schema(self):
        """Resolve the configuration against the introspected schema"""

        schema = _schema(self.data, tuple(sorted(self.headers.items())),
                         self.timeout)
        if schema.query_type is None:
            _config_error('endpoint has no query type')

        field = schema.query_type.fields.get(self.collection)
        if field is None:
            _config_error(f'collection {self.collection} not found')

        self._node_type = self._node(field)
        self._filter_type = self._check_arguments(field)
        self._mapping = self._map_fields()
        self._geometry_paths()
        self._check_bbox_filter()
        self._build_sorting(field)

    def _node(self, field):
        """
        Node object type of the collection field (list shape)

        :param field: collection field on the query type

        :returns: graphql.GraphQLObjectType
        """

        type_ = field.type
        if is_non_null_type(type_):
            type_ = type_.of_type
        if not is_list_type(type_):
            _config_error(f'collection {self.collection} must return a list')

        node = get_named_type(type_)
        if not is_object_type(node):
            _config_error(f'collection {self.collection} must list objects')

        return node

    def _check_arguments(self, field):
        """
        Validate the collection field arguments

        :param field: collection field on the query type

        :returns: filter input type, or None
        """

        for name in ('first', 'offset'):
            if name not in field.args:
                _config_error(f'collection must accept {name}')
        self._limit_decl = str(field.args['first'].type)
        self._offset_decl = str(field.args['offset'].type)

        for name, arg in field.args.items():
            if name in ('filter', 'orderBy'):
                if is_required_argument(arg):
                    _config_error(f'argument {name} must be optional')
            elif name not in ('first', 'offset') and is_required_argument(arg):
                _config_error(f'required argument {name} not supported')

        self._filter_decl = None
        if 'filter' in field.args:
            filter_type = get_named_type(field.args['filter'].type)
            if is_input_object_type(filter_type):
                self._filter_decl = str(field.args['filter'].type)
                return filter_type

        return None

    def _map_fields(self):
        """
        Map feature properties to fields of the node type

        :returns: dict of property name to (path tuple, GraphQL type)
        """

        if self.field_mapping:
            mapping = {prop: self._resolve_path(path, f'field_mapping.{prop}')
                       for prop, path in self.field_mapping.items()}
        else:
            geometry = {self.geometry_x, self.geometry_y,
                        self.geometry_geojson}
            mapping = {}
            for name, field in self._node_type.fields.items():
                named = get_named_type(field.type)
                if (name in geometry
                        or not (is_scalar_type(named) or is_enum_type(named))
                        or any(is_required_argument(arg)
                               for arg in field.args.values())):
                    continue
                mapping[name] = ((name,), field.type)

        if self.properties:
            missing = [str(p) for p in self.properties
                       if not isinstance(p, str) or p not in mapping]
            if missing:
                _config_error(f"properties not found: {', '.join(missing)}")
            mapping = {p: mapping[p] for p in self.properties}

        if self.id_field not in mapping:
            _config_error(f'id_field {self.id_field} is not exposed')
        if self.title_field and self.title_field not in mapping:
            _config_error(f'title_field {self.title_field} is not exposed')

        return mapping

    def _resolve_path(self, path, key):
        """
        Resolve a dotted path to a scalar leaf on the node type

        :param path: dotted GraphQL path
        :param key: configuration key, for the error message

        :returns: tuple of (path tuple, GraphQL type at the leaf)
        """

        parts = path.split('.')
        type_ = self._node_type
        leaf = None
        for depth, part in enumerate(parts):
            if not is_object_type(type_):
                _config_error(f'{key}: {path} is not reachable')
            field = type_.fields.get(part)
            if field is None:
                _config_error(f'{key}: field {part} not found')
            if any(is_required_argument(arg)
                   for arg in field.args.values()):
                _config_error(f'{key}: field {part} needs arguments')
            leaf = field.type
            inner = leaf.of_type if is_non_null_type(leaf) else leaf
            if is_list_type(inner) and depth < len(parts) - 1:
                _config_error(f'{key}: {part} is a list')
            type_ = get_named_type(leaf)

        if not (is_scalar_type(type_) or is_enum_type(type_)):
            _config_error(f'{key}: {path} is not a scalar field')

        return tuple(parts), leaf

    def _geometry_paths(self):
        """Resolve the configured geometry fields against the node type"""

        self._geom_x = self._geom_y = self._geom_geojson = None
        if self.geometry_x:
            self._geom_x = self._resolve_path(
                self.geometry_x, 'geometry.x_field')[0]
            self._geom_y = self._resolve_path(
                self.geometry_y, 'geometry.y_field')[0]
        if self.geometry_geojson:
            self._geom_geojson = self._resolve_path(
                self.geometry_geojson, 'geometry.geojson_field')[0]

    def _check_bbox_filter(self):
        """Validate bbox_filter against the filter input type"""

        if self.bbox_field is None:
            return
        if self._filter_type is None:
            _config_error('bbox_filter needs a filter argument')

        field = self._filter_type.fields.get(self.bbox_field)
        if field is None:
            _config_error(f'bbox_filter field {self.bbox_field} not found')

        operators = get_named_type(field.type)
        if (not is_input_object_type(operators)
                or self.bbox_operator not in operators.fields):
            _config_error(
                f'bbox_filter operator {self.bbox_operator} not supported')

    def _build_sorting(self, field):
        """
        Build the sort map from the orderBy enum

        :param field: collection field on the query type
        """

        self._sort_map = {}
        self._order_decl = None

        if 'orderBy' in field.args:
            arg_type = field.args['orderBy'].type
            if is_non_null_type(arg_type):
                arg_type = arg_type.of_type
            order_type = get_named_type(arg_type)
            if is_list_type(arg_type) and is_enum_type(order_type):
                self._order_decl = str(field.args['orderBy'].type)
                self._sort_map = _order_map(self._mapping, order_type)

        if self.id_field not in self._sort_map:
            LOGGER.warning(f'no orderBy value for {self.id_field}: '
                           'paging order is not guaranteed')

    def _selection(self, select_properties, skip_geometry):
        """
        Properties to return and field paths to request upstream

        :param select_properties: list of requested property names
        :param skip_geometry: bool of whether to skip geometry

        :returns: tuple of (property names, field paths)
        """

        props = [p for p in self._mapping
                 if not select_properties or p in select_properties]

        paths = [self._mapping[p][0] for p in props]
        if self.id_field not in props:
            paths.append(self._mapping[self.id_field][0])
        if not skip_geometry:
            for path in (self._geom_x, self._geom_y, self._geom_geojson):
                if path:
                    paths.append(path)

        return props, paths

    def _document(self, paths, with_sort, with_filter=False):
        """
        Build the GraphQL document, one variable and argument per line

        :param paths: field paths to select
        :param with_sort: include the orderBy variable
        :param with_filter: include the filter variable

        :returns: str of GraphQL document
        """

        variables = [f'$limit: {self._limit_decl}',
                     f'$offset: {self._offset_decl}']
        arguments = ['first: $limit', 'offset: $offset']
        if with_filter:
            variables.append(f'$filter: {self._filter_decl}')
            arguments.append('filter: $filter')
        if with_sort:
            variables.append(f'$sortby: {self._order_decl}')
            arguments.append('orderBy: $sortby')

        tree = {}
        for path in paths:
            node = tree
            for part in path:
                node = node.setdefault(part, {})

        lines = ['query (']
        lines += [f'  {variable}' for variable in variables]
        lines += [') {', f'  {self.collection}(']
        lines += [f'    {argument}' for argument in arguments]
        lines += ['  ) {']
        lines += _render_tree(tree, '    ')
        lines += ['  }', '}']

        return '\n'.join(lines)

    def _order(self, sortby):
        """
        Map client sortby to orderBy enum values, id as tiebreaker

        :param sortby: list of dicts (property, order)

        :returns: list of orderBy enum values
        """

        order = []
        used = set()
        for sort in sortby:
            prop = sort['property']
            pair = self._sort_map.get(prop)
            if pair is None:
                msg = f'property {prop} is not sortable'
                LOGGER.error(msg)
                raise ProviderInvalidQueryError(
                    user_msg='property is not sortable')
            order.append(pair[0] if sort['order'] == '+' else pair[1])
            used.add(prop)

        pair = self._sort_map.get(self.id_field)
        if pair and self.id_field not in used:
            order.append(pair[0])

        return order

    def _post(self, document, variables):
        """
        POST the document and return the collection nodes

        :param document: GraphQL document
        :param variables: dict of variables

        :returns: list of reply nodes
        """

        data = _post_graphql(self._session, self.data, self.headers,
                             self.timeout, document, variables)
        nodes = data.get(self.collection)
        if not isinstance(nodes, list):
            msg = f'Unexpected reply shape from {self.data}'
            LOGGER.error(msg)
            raise ProviderQueryError(msg)

        return nodes

    def _feature(self, node, props, skip_geometry):
        """
        Build a GeoJSON feature from a reply node

        :param node: reply node
        :param props: property names to return
        :param skip_geometry: bool of whether to skip geometry

        :returns: dict of GeoJSON feature
        """

        id_ = _value_at(node, self._mapping[self.id_field][0])
        if id_ is None:
            msg = 'id missing in reply'
            LOGGER.error(msg)
            raise ProviderQueryError(msg)

        feature = {'type': 'Feature', 'id': id_, 'geometry': None,
                   'properties': {}}
        if not skip_geometry:
            feature['geometry'] = self._geometry(node)
        for prop in props:
            feature['properties'][prop] = _value_at(
                node, self._mapping[prop][0])

        return feature

    def _geometry(self, node):
        """
        Geometry of a reply node

        :param node: reply node

        :returns: dict of GeoJSON geometry, or None
        """

        if self._geom_geojson:
            value = _value_at(node, self._geom_geojson)
            if (isinstance(value, dict)
                    and value.get('type') in GEOJSON_TYPES
                    and ('coordinates' in value or 'geometries' in value)):
                return value
            return None

        if self._geom_x:
            try:
                x = float(_value_at(node, self._geom_x))
                y = float(_value_at(node, self._geom_y))
            except (TypeError, ValueError):
                LOGGER.debug('node without a valid point geometry')
                return None
            if not (math.isfinite(x) and math.isfinite(y)):
                LOGGER.debug('node with non-finite coordinates')
                return None
            return {'type': 'Point', 'coordinates': [x, y]}

        return None

    def _check_unsupported(self, resulttype, bbox, datetime_, properties,
                           q, filterq):
        """
        Reject query parameters version 1 does not support

        :param resulttype: results or hits
        :param bbox: bounding box
        :param datetime_: temporal filter
        :param properties: list of tuples (name, value)
        :param q: full-text search term(s)
        :param filterq: CQL filter
        """

        if resulttype == 'hits':
            _unsupported('resulttype hits is not supported')
        if bbox:
            _unsupported('bbox is not supported')
        if datetime_ is not None:
            _unsupported('datetime is not supported')
        if properties:
            _unsupported('property filters are not supported')
        if q is not None:
            _unsupported('q is not supported')
        if filterq is not None:
            _unsupported('CQL filters are not supported')

    @crs_transform
    def query(self, offset=0, limit=10, resulttype='results',
              bbox=[], datetime_=None, properties=[], sortby=[],
              select_properties=[], skip_geometry=False, q=None, **kwargs):
        """
        GraphQL query

        :param offset: starting record to return (default 0)
        :param limit: number of records to return (default 10)
        :param resulttype: return results or hit limit (default results)
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param datetime_: temporal (datestamp or extent)
        :param properties: list of tuples (name, value)
        :param sortby: list of dicts (property, order)
        :param select_properties: list of property names
        :param skip_geometry: bool of whether to skip geometry (default False)
        :param q: full-text search term(s)

        :returns: dict of GeoJSON FeatureCollection
        """

        self._check_unsupported(resulttype, bbox, datetime_, properties,
                                q, kwargs.get('filterq'))
        if offset + limit > INT32_MAX:
            msg = f'offset {offset} beyond Int32'
            LOGGER.error(msg)
            raise ProviderInvalidQueryError(user_msg='offset too large')

        order = self._order(sortby)
        props, paths = self._selection(select_properties, skip_geometry)
        document = self._document(paths, with_sort=bool(order))
        base = {'sortby': order} if order else {}

        features = []
        remaining = limit
        position = offset
        while remaining > 0:
            variables = {'limit': min(self.page_size, remaining),
                         'offset': position, **base}
            nodes = self._post(document, variables)
            if len(nodes) > variables['limit']:
                LOGGER.debug('reply longer than requested, truncating')
                nodes = nodes[:variables['limit']]
            for node in nodes:
                if node is None:
                    msg = 'null node in reply'
                    LOGGER.error(msg)
                    raise ProviderQueryError(msg)
                features.append(self._feature(node, props, skip_geometry))
            if len(nodes) < variables['limit']:
                break
            position += len(nodes)
            remaining -= len(nodes)

        return {'type': 'FeatureCollection', 'features': features,
                'numberReturned': len(features)}

    @crs_transform
    def get(self, identifier, **kwargs):
        """
        Query the provider by identifier

        :param identifier: feature id

        :returns: dict of single GeoJSON feature
        """

        raise NotImplementedError()

    def __repr__(self):
        return f'<GraphQLProvider> {self.data}'
