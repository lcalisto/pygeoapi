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

import logging
import re
from urllib.parse import urlparse

from pygeoapi.crs import crs_transform
from pygeoapi.provider.base import BaseProvider, ProviderGenericError

LOGGER = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT = 30

GRAPHQL_NAME = re.compile(r'[_A-Za-z][_0-9A-Za-z]*')

GRAPHQL_KEYS = {'collection', 'geometry', 'bbox_filter', 'page_size',
                'field_mapping', 'headers', 'timeout'}


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
        self.get_fields()

    def get_fields(self):
        """
        Get provider field information (names, types)

        :returns: dict of fields
        """

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

        raise NotImplementedError()

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
