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

import json
import logging
from copy import deepcopy
from http import HTTPStatus
from unittest import mock

import pyproj
import pytest
import requests
from graphql import (build_client_schema, build_schema,
                     get_introspection_query, graphql_sync, parse, validate)

from pygeoapi.api import API
from pygeoapi.api.itemtypes import get_collection_queryables
from pygeoapi.crs import CrsTransformSpec, get_crs
from pygeoapi.openapi import get_oas
from pygeoapi.plugin import load_plugin
from pygeoapi.provider import graphql_
from pygeoapi.provider.base import (ProviderConnectionError,
                                    ProviderGenericError,
                                    ProviderInvalidQueryError,
                                    ProviderQueryError)
from pygeoapi.provider.graphql_ import GraphQLProvider
from pygeoapi.util import yaml_load

from ..util import get_test_file_path, mock_api_request

WOSIS_URL = 'https://wosis.example.org/graphql'
SYNTH_URL = 'https://graphql.example.org/graphql'
INTROSPECTION = get_introspection_query(descriptions=False)

THINGS_SDL = '''
    enum ThingsOrderBy { ID_ASC ID_DESC NAME_ASC NAME_DESC }
    type Query {
      things(first: Int, offset: Int, orderBy: [ThingsOrderBy!]): [Thing]
    }
    type Thing { id: Int name: String }
'''

POINTS_SDL = '''
    type Query { points(first: Int, offset: Int): [Pt] }
    type Pt { id: Int x: Float y: Float }
'''


class FakeResponse:
    """Minimal stand-in for requests.Response"""

    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return deepcopy(self._body)


class FakeTransport:
    """Strict record-and-replay stand-in for requests.Session.post"""

    def __init__(self):
        self.replies = {}
        self.calls = []

    @staticmethod
    def key(url, document, variables):
        canonical = json.dumps(variables, sort_keys=True, allow_nan=False)
        return url, ' '.join(document.split()), canonical

    def add(self, url, document, variables, body, status_code=200):
        self.replies[self.key(url, document, variables)] = (body, status_code)

    def __call__(self, url, json=None, **kwargs):
        self.calls.append({'url': url, 'payload': json, 'kwargs': kwargs})
        payload = json or {}
        key = self.key(url, payload.get('query', ''),
                       payload.get('variables'))
        if key not in self.replies:
            raise AssertionError(f'unexpected GraphQL request: {key}')
        body, status_code = self.replies[key]
        return FakeResponse(body, status_code)


def synthetic(transport, sdl, url=SYNTH_URL):
    """Serve an SDL-built schema as the introspection reply for url"""

    result = graphql_sync(build_schema(sdl), INTROSPECTION)
    assert result.errors is None, result.errors
    transport.add(url, INTROSPECTION, None, {'data': result.data})
    return url


@pytest.fixture()
def transport():
    fake = FakeTransport()
    with open(get_test_file_path('data/graphql/wosis_schema.json')) as fh:
        fake.add(WOSIS_URL, INTROSPECTION, None, json.load(fh))
    path = get_test_file_path('data/graphql/wosis_interactions.json')
    with open(path) as fh:
        for interaction in json.load(fh):
            fake.add(WOSIS_URL, interaction['document'],
                     interaction['variables'], interaction['body'],
                     interaction['status'])
    return fake


def data_calls(transport):
    """Variables of the non-introspection calls, in order"""

    return [call['payload'].get('variables') for call in transport.calls
            if '__schema' not in call['payload']['query']]


def recorded_interaction(name):
    """One recorded WoSIS interaction, by name"""

    path = get_test_file_path('data/graphql/wosis_interactions.json')
    with open(path) as fh:
        return next(ex for ex in json.load(fh) if ex['name'] == name)


@pytest.fixture(autouse=True)
def offline(transport):
    graphql_._schema.cache_clear()
    blocked = mock.patch(
        'requests.adapters.HTTPAdapter.send',
        side_effect=AssertionError('network access blocked in tests'))
    posted = mock.patch(
        'pygeoapi.provider.graphql_.requests.Session.post', new=transport)
    with blocked, posted:
        yield transport
    graphql_._schema.cache_clear()


@pytest.fixture()
def config():
    # WoSIS, ISRIC World Soil Information
    # schema and interactions recorded from
    # https://graphql.isric.org/wosis/graphql (CC BY 4.0, see
    # tests/data/README.md); tests never touch the network
    return {
        'name': 'GraphQL',
        'type': 'feature',
        'data': WOSIS_URL,
        'id_field': 'profileId',
        'title_field': 'profileCode',
        'graphql': {
            'collection': 'wosisLatestProfiles',
            'geometry': {
                'x_field': 'longitude',
                'y_field': 'latitude'
            },
            'bbox_filter': {
                'field': 'geom',
                'operator': 'bboxIntersects2D'
            },
            'page_size': 100
        }
    }


@pytest.fixture()
def things_config():
    return {
        'name': 'GraphQL',
        'type': 'feature',
        'data': SYNTH_URL,
        'id_field': 'id',
        'graphql': {
            'collection': 'things'
        }
    }


@pytest.fixture()
def graphql_config(config):
    with open(get_test_file_path('pygeoapi-test-config.yml')) as fh:
        api_config = yaml_load(fh)
    resource = deepcopy(api_config['resources']['obs'])
    resource['title'] = 'WoSIS profiles'
    resource['providers'] = [config]
    api_config['resources'] = {'wosis-profiles': resource}
    return api_config


@pytest.fixture()
def graphql_api(graphql_config):
    with open(get_test_file_path('pygeoapi-test-openapi.yml')) as fh:
        openapi = yaml_load(fh)
    return API(graphql_config, openapi)


def test_plugin_registry(config):
    provider = load_plugin('provider', config)

    assert isinstance(provider, GraphQLProvider)


def test_config_parsed(config):
    p = GraphQLProvider(config)

    assert p.collection == 'wosisLatestProfiles'
    assert p.geometry_x == 'longitude'
    assert p.geometry_y == 'latitude'
    assert p.geometry_geojson is None
    assert p.bbox_field == 'geom'
    assert p.bbox_operator == 'bboxIntersects2D'
    assert p.page_size == 100
    expected = '<GraphQLProvider> https://wosis.example.org/graphql'
    assert repr(p) == expected


def test_defaults(config):
    config['graphql'] = {'collection': 'wosisLatestProfiles'}
    p = GraphQLProvider(config)

    assert p.page_size == 100
    assert p.timeout == 30
    assert p.headers == {}
    assert p.field_mapping == {}
    assert p.geometry_x is None
    assert p.geometry_y is None
    assert p.geometry_geojson is None
    assert p.bbox_field is None
    assert p.bbox_operator is None
    assert list(p.fields) == ['profileId', 'profileCode', 'countryName',
                              'longitude', 'latitude']
    assert p.fields['longitude'] == {'type': 'number'}


def test_config_not_mutated(config):
    original = deepcopy(config)
    GraphQLProvider(config)

    assert config == original


def test_headers_coerced(config):
    config['graphql']['headers'] = {'x-api-key': 5}
    p = GraphQLProvider(config)

    assert p.headers == {'x-api-key': '5'}


@pytest.mark.parametrize('data', [
    'ftp://example.com/graphql',
    '/data/wosis.json',
    123
])
def test_data_must_be_http(config, data):
    config['data'] = data

    with pytest.raises(ProviderGenericError, match='http'):
        GraphQLProvider(config)


def test_id_field_required(config):
    del config['id_field']

    with pytest.raises(ProviderGenericError, match='id_field'):
        GraphQLProvider(config)


def test_editable_rejected(config):
    config['editable'] = True

    with pytest.raises(ProviderGenericError, match='editable'):
        GraphQLProvider(config)


def test_time_field_rejected(config):
    config['time_field'] = 'date'

    with pytest.raises(ProviderGenericError, match='time_field'):
        GraphQLProvider(config)


def test_graphql_block_required(config):
    del config['graphql']

    with pytest.raises(ProviderGenericError, match='graphql block'):
        GraphQLProvider(config)


def test_graphql_block_not_mapping(config):
    config['graphql'] = 'wosisLatestProfiles'

    with pytest.raises(ProviderGenericError, match='graphql block'):
        GraphQLProvider(config)


def test_collection_required(config):
    del config['graphql']['collection']

    with pytest.raises(ProviderGenericError, match='collection'):
        GraphQLProvider(config)


@pytest.mark.parametrize('key', ['condition', 'pagination', 'query_template'])
def test_unknown_graphql_key(config, key):
    config['graphql'][key] = 'x'

    with pytest.raises(ProviderGenericError, match=key):
        GraphQLProvider(config)


@pytest.mark.parametrize('collection', ['a.b', ''])
def test_collection_invalid_name(config, collection):
    config['graphql']['collection'] = collection

    with pytest.raises(ProviderGenericError, match='collection'):
        GraphQLProvider(config)


@pytest.mark.parametrize('geometry', ['geom', {'x_field': 'longitude'}])
def test_geometry_invalid(config, geometry):
    config['graphql']['geometry'] = geometry

    with pytest.raises(ProviderGenericError, match='geometry'):
        GraphQLProvider(config)


def test_geometry_geojson_path(config):
    config['graphql']['geometry'] = {'geojson_field': 'geom.geojson'}
    p = GraphQLProvider(config)

    assert p.geometry_geojson == 'geom.geojson'
    assert p.geometry_x is None
    assert p.geometry_y is None


@pytest.mark.parametrize('bbox_filter', ['geom', {'field': 'geom'}])
def test_bbox_filter_invalid(config, bbox_filter):
    config['graphql']['bbox_filter'] = bbox_filter

    with pytest.raises(ProviderGenericError, match='bbox_filter'):
        GraphQLProvider(config)


@pytest.mark.parametrize('key', ['page_size', 'timeout'])
@pytest.mark.parametrize('value', [0, 'many', True])
def test_positive_int_keys(config, key, value):
    config['graphql'][key] = value

    with pytest.raises(ProviderGenericError, match=key):
        GraphQLProvider(config)


@pytest.mark.parametrize('mapping', [
    'countryName',
    {},
    {'country': 'country name'},
    {5: 'countryName'}
])
def test_field_mapping_invalid(config, mapping):
    config['graphql']['field_mapping'] = mapping

    with pytest.raises(ProviderGenericError, match='field_mapping'):
        GraphQLProvider(config)


@pytest.mark.parametrize('headers', [
    'token',
    {'x-api-key': {}},
    {'x-api-key': None},
    {5: 'value'}
])
def test_headers_invalid(config, headers):
    config['graphql']['headers'] = headers

    with pytest.raises(ProviderGenericError, match='headers'):
        GraphQLProvider(config)


def test_introspection_request(config, transport):
    GraphQLProvider(config)

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call['url'] == WOSIS_URL
    assert call['payload']['query'] == INTROSPECTION
    assert 'variables' not in call['payload']
    assert call['kwargs']['timeout'] == 30
    assert call['kwargs']['allow_redirects'] is False
    assert call['kwargs']['headers'] == {}


def test_schema_cache_shared(config, transport):
    GraphQLProvider(config)
    GraphQLProvider(config)

    assert len(transport.calls) == 1


def test_schema_cache_key_headers(config, transport):
    GraphQLProvider(config)
    config['graphql']['headers'] = {'x-api-key': 'k'}
    GraphQLProvider(config)

    assert len(transport.calls) == 2
    assert transport.calls[1]['kwargs']['headers'] == {'x-api-key': 'k'}


def test_schema_cache_failure_not_cached(config, transport):
    transport.add(WOSIS_URL, INTROSPECTION, None, {}, status_code=500)

    with pytest.raises(ProviderConnectionError):
        GraphQLProvider(config)

    with open(get_test_file_path('data/graphql/wosis_schema.json')) as fh:
        transport.add(WOSIS_URL, INTROSPECTION, None, json.load(fh))
    p = GraphQLProvider(config)

    assert p.fields
    assert len(transport.calls) == 2


def test_fields_from_schema(config):
    p = GraphQLProvider(config)

    assert list(p.fields) == ['profileId', 'profileCode', 'countryName']
    assert p.fields['profileId'] == {'type': 'integer'}
    assert p.fields['profileCode'] == {'type': 'string'}
    assert p.fields['countryName'] == {'type': 'string'}


def test_fields_fresh_dicts(config):
    p = GraphQLProvider(config)
    fields = p.get_fields()
    fields['profileId']['type'] = 'mutated'

    assert p.get_fields()['profileId'] == {'type': 'integer'}


def test_field_mapping_resolved(config):
    config['id_field'] = 'code'
    config['title_field'] = 'code'
    config['graphql']['field_mapping'] = {
        'code': 'profileCode',
        'geojson': 'geom.geojson'
    }
    p = GraphQLProvider(config)

    assert p.fields == {'code': {'type': 'string'},
                        'geojson': {'type': 'object'}}


@pytest.mark.parametrize('path,error', [
    ('geom.nope', 'not found'),
    ('geom', 'not a scalar'),
    ('profileCode.geojson', 'not reachable')
])
def test_field_mapping_bad_path(config, path, error):
    config['graphql']['field_mapping'] = {'profileId': 'profileId',
                                          'broken': path}

    with pytest.raises(ProviderGenericError, match=error):
        GraphQLProvider(config)


def test_properties_restricts_fields(config):
    config['properties'] = ['countryName', 'profileCode', 'profileId']
    p = GraphQLProvider(config)

    assert list(p.fields) == ['countryName', 'profileCode', 'profileId']


@pytest.mark.parametrize('properties', [
    ['profileId', 'profileCode', 'elevation'],
    ['profileId', 'profileCode', 2024]
])
def test_properties_unknown(config, properties):
    config['properties'] = properties

    with pytest.raises(ProviderGenericError, match='properties'):
        GraphQLProvider(config)


@pytest.mark.parametrize('kept,error', [
    (['profileCode', 'countryName'], 'id_field'),
    (['profileId', 'countryName'], 'title_field')
])
def test_properties_must_keep_id_and_title(config, kept, error):
    config['properties'] = kept

    with pytest.raises(ProviderGenericError, match=error):
        GraphQLProvider(config)


def test_id_field_not_on_schema(config):
    config['id_field'] = 'identifier'

    with pytest.raises(ProviderGenericError, match='id_field'):
        GraphQLProvider(config)


def test_title_field_not_on_schema(config):
    config['title_field'] = 'label'

    with pytest.raises(ProviderGenericError, match='title_field'):
        GraphQLProvider(config)


def test_collection_not_on_schema(config):
    config['graphql']['collection'] = 'wosisLatestSites'

    with pytest.raises(ProviderGenericError, match='not found'):
        GraphQLProvider(config)


def test_geometry_not_on_schema(config):
    config['graphql']['geometry'] = {'x_field': 'lon', 'y_field': 'latitude'}

    with pytest.raises(ProviderGenericError, match='geometry.x_field'):
        GraphQLProvider(config)


@pytest.mark.parametrize('bbox_filter,error', [
    ({'field': 'elevation', 'operator': 'bboxIntersects2D'}, 'field'),
    ({'field': 'geom', 'operator': 'nope'}, 'operator')
])
def test_bbox_filter_not_on_filter_type(config, bbox_filter, error):
    config['graphql']['bbox_filter'] = bbox_filter

    with pytest.raises(ProviderGenericError, match=f'bbox_filter {error}'):
        GraphQLProvider(config)


def test_bbox_filter_needs_filter_argument(transport, things_config):
    synthetic(transport, '''
        type Query { things(first: Int, offset: Int): [Thing] }
        type Thing { id: Int }
    ''')
    things_config['graphql']['bbox_filter'] = {'field': 'geom',
                                               'operator': 'intersects'}

    with pytest.raises(ProviderGenericError, match='filter argument'):
        GraphQLProvider(things_config)


def test_type_mapping(transport, things_config):
    synthetic(transport, '''
        scalar Date
        scalar Datetime
        scalar JSON
        enum Color { RED GREEN }
        type Query { things(first: Int, offset: Int): [Thing] }
        type Thing {
          id: Int!
          name: String
          ok: Boolean
          day: Date
          when: Datetime
          blob: JSON
          tags: [String]
          color: Color
          n2x: Float
          computed(arg: Int!): String
        }
    ''')
    p = GraphQLProvider(things_config)

    assert p.fields == {
        'id': {'type': 'integer'},
        'name': {'type': 'string'},
        'ok': {'type': 'boolean'},
        'day': {'type': 'string', 'format': 'date'},
        'when': {'type': 'string', 'format': 'date-time'},
        'blob': {'type': 'object'},
        'tags': {'type': 'array'},
        'color': {'type': 'string'},
        'n2x': {'type': 'number'}
    }


@pytest.mark.parametrize('arguments,error', [
    ('first: Int', 'accept offset'),
    ('offset: Int', 'accept first'),
    ('first: Int, offset: Int, token: String!', 'required argument token')
])
def test_collection_arguments(transport, things_config, arguments, error):
    synthetic(transport, f'''
        type Query {{ things({arguments}): [Thing] }}
        type Thing {{ id: Int }}
    ''')

    with pytest.raises(ProviderGenericError, match=error):
        GraphQLProvider(things_config)


@pytest.mark.parametrize('argument,sdl_arg', [
    ('filter', 'filter: ThingFilter!'),
    ('orderBy', 'orderBy: [ThingsOrderBy!]!')
])
def test_filter_and_order_must_be_optional(transport, things_config,
                                           argument, sdl_arg):
    synthetic(transport, f'''
        input IntEq {{ equalTo: Int }}
        input ThingFilter {{ id: IntEq }}
        enum ThingsOrderBy {{ ID_ASC ID_DESC }}
        type Query {{
          things(first: Int, offset: Int, {sdl_arg}): [Thing]
        }}
        type Thing {{ id: Int }}
    ''')

    with pytest.raises(ProviderGenericError,
                       match=f'{argument} must be optional'):
        GraphQLProvider(things_config)


@pytest.mark.parametrize('path,error', [
    ('calc.name', 'needs arguments'),
    ('rels.name', 'is a list')
])
def test_field_mapping_path_guards(transport, things_config, path, error):
    synthetic(transport, '''
        type Query { things(first: Int, offset: Int): [Thing] }
        type Thing { id: Int calc(x: Int!): Sub rels: [Sub] }
        type Sub { name: String }
    ''')
    things_config['graphql']['field_mapping'] = {'id': 'id', 'bad': path}

    with pytest.raises(ProviderGenericError, match=error):
        GraphQLProvider(things_config)


def test_geojson_source_excluded(transport, things_config):
    synthetic(transport, '''
        scalar GeoJSON
        type Query { things(first: Int, offset: Int): [Thing] }
        type Thing { id: Int geo: GeoJSON name: String }
    ''')
    things_config['graphql']['geometry'] = {'geojson_field': 'geo'}
    p = GraphQLProvider(things_config)

    assert list(p.fields) == ['id', 'name']


def test_collection_not_a_list(transport, things_config):
    synthetic(transport, '''
        type Query { things(first: Int, offset: Int): Thing }
        type Thing { id: Int }
    ''')

    with pytest.raises(ProviderGenericError, match='must return a list'):
        GraphQLProvider(things_config)


def test_collection_lists_scalars(transport, things_config):
    synthetic(transport, '''
        type Query { things(first: Int, offset: Int): [String] }
    ''')

    with pytest.raises(ProviderGenericError, match='must list objects'):
        GraphQLProvider(things_config)


def test_transport_error(config):
    failing = mock.patch(
        'pygeoapi.provider.graphql_.requests.Session.post',
        side_effect=requests.ConnectionError('connection refused'))

    with failing, pytest.raises(ProviderConnectionError):
        GraphQLProvider(config)


def test_redirect_rejected(config, transport):
    transport.add(WOSIS_URL, INTROSPECTION, None, {}, status_code=302)

    with pytest.raises(ProviderConnectionError, match='redirect'):
        GraphQLProvider(config)


def test_non_json_reply(config):
    raw = FakeResponse(None)
    raw.json = mock.Mock(side_effect=ValueError('no json'))
    not_json = mock.patch(
        'pygeoapi.provider.graphql_.requests.Session.post', return_value=raw)

    with not_json, pytest.raises(ProviderConnectionError, match='JSON'):
        GraphQLProvider(config)


def test_graphql_errors_reply(config, transport):
    transport.add(WOSIS_URL, INTROSPECTION, None,
                  {'errors': [{'message': 'introspection disabled'}]})

    with pytest.raises(ProviderQueryError, match='GraphQL errors'):
        GraphQLProvider(config)


def test_graphql_errors_with_400(config, transport):
    # WoSIS validation errors: HTTP 400, errors array, no data
    transport.add(WOSIS_URL, INTROSPECTION, None,
                  {'errors': [{'message': 'first is required'}]},
                  status_code=400)

    with pytest.raises(ProviderQueryError, match='GraphQL errors'):
        GraphQLProvider(config)


def test_bad_reply_shape(config, transport):
    transport.add(WOSIS_URL, INTROSPECTION, None, {'data': None})

    with pytest.raises(ProviderQueryError, match='reply shape'):
        GraphQLProvider(config)


def test_query_defaults(config, transport):
    result = GraphQLProvider(config).query()

    assert result['type'] == 'FeatureCollection'
    assert result['numberReturned'] == 10
    assert len(result['features']) == 10
    assert 'numberMatched' not in result
    feature = result['features'][0]
    assert feature['type'] == 'Feature'
    assert isinstance(feature['id'], int)
    assert list(feature['properties']) == ['profileId', 'profileCode',
                                           'countryName']
    assert feature['geometry']['type'] == 'Point'
    row = recorded_interaction('defaults')['body']['data']
    row = row['wosisLatestProfiles'][0]
    expected = [row['longitude'], row['latitude']]
    assert feature['geometry']['coordinates'] == expected
    assert data_calls(transport) == [
        {'limit': 10, 'offset': 0, 'sortby': ['PROFILE_ID_ASC']}]
    # layout pinned: one variable and one argument per line
    document = transport.calls[-1]['payload']['query']
    assert document == recorded_interaction('defaults')['document']


def test_query_offset_limit(config, transport):
    result = GraphQLProvider(config).query(offset=5, limit=5)

    assert result['numberReturned'] == 5
    assert data_calls(transport) == [
        {'limit': 5, 'offset': 5, 'sortby': ['PROFILE_ID_ASC']}]


def test_query_chunking(config, transport):
    config['graphql']['page_size'] = 2
    result = GraphQLProvider(config).query(limit=5)

    assert result['numberReturned'] == 5
    assert len({feature['id'] for feature in result['features']}) == 5
    assert [(v['limit'], v['offset']) for v in data_calls(transport)] == [
        (2, 0), (2, 2), (1, 4)]


def test_query_short_page(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 5, 'offset': 0, 'sortby': ['ID_ASC']},
                  {'data': {'things': [{'id': 1, 'name': 'a'},
                                       {'id': 2, 'name': 'b'},
                                       {'id': 3, 'name': 'c'}]}})
    result = p.query(limit=5)

    assert result['numberReturned'] == 3
    assert len(data_calls(transport)) == 1


def test_query_empty(config):
    result = GraphQLProvider(config).query(offset=9000000)

    assert result['numberReturned'] == 0
    assert result['features'] == []


def test_query_null_node_rejected(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 3, 'offset': 0, 'sortby': ['ID_ASC']},
                  {'data': {'things': [{'id': 1, 'name': 'a'}, None,
                                       {'id': 3, 'name': 'c'}]}})

    with pytest.raises(ProviderQueryError, match='null node'):
        p.query(limit=3)


def test_query_oversized_page_truncated(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 2, 'offset': 0, 'sortby': ['ID_ASC']},
                  {'data': {'things': [{'id': n, 'name': 'x'}
                                       for n in range(1, 6)]}})
    result = p.query(limit=2)

    assert result['numberReturned'] == 2
    assert [feature['id'] for feature in result['features']] == [1, 2]
    assert len(data_calls(transport)) == 1


def test_geometry_geojson(config):
    config['graphql']['geometry'] = {'geojson_field': 'geom.geojson'}
    result = GraphQLProvider(config).query(limit=5)
    geometry = result['features'][0]['geometry']

    assert geometry['type'] == 'Point'
    assert 'coordinates' in geometry


def test_geometry_missing_null(transport, things_config):
    synthetic(transport, POINTS_SDL)
    things_config['graphql'] = {
        'collection': 'points',
        'geometry': {'x_field': 'x', 'y_field': 'y'}
    }
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=False)
    transport.add(SYNTH_URL, document, {'limit': 4, 'offset': 0},
                  {'data': {'points': [{'id': 1, 'x': None, 'y': 2.5},
                                       {'id': 2, 'x': 'abc', 'y': 1.0},
                                       {'id': 3, 'x': 'NaN', 'y': 1.0},
                                       {'id': 4, 'x': 1.0, 'y': 2.0}]}})
    result = p.query(limit=4)
    geometries = [feature['geometry'] for feature in result['features']]

    assert geometries[:3] == [None, None, None]
    assert geometries[3]['coordinates'] == [1.0, 2.0]


def test_geometry_geojson_invalid(transport, things_config):
    synthetic(transport, '''
        scalar GeoJSON
        type Query { blobs(first: Int, offset: Int): [Blob] }
        type Blob { id: Int geo: GeoJSON }
    ''')
    things_config['graphql'] = {
        'collection': 'blobs',
        'geometry': {'geojson_field': 'geo'}
    }
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=False)
    transport.add(SYNTH_URL, document, {'limit': 4, 'offset': 0},
                  {'data': {'blobs': [
                      {'id': 1, 'geo': {}},
                      {'id': 2, 'geo': {'type': 'Point'}},
                      {'id': 3, 'geo': 'POINT(1 1)'},
                      {'id': 4, 'geo': {'type': 'Point',
                                        'coordinates': [1.0, 2.0]}}]}})
    result = p.query(limit=4)
    geometries = [feature['geometry'] for feature in result['features']]

    assert geometries[:3] == [None, None, None]
    assert geometries[3] == {'type': 'Point', 'coordinates': [1.0, 2.0]}


def test_id_zero_accepted(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 1, 'offset': 0, 'sortby': ['ID_ASC']},
                  {'data': {'things': [{'id': 0, 'name': 'zero'}]}})
    result = p.query(limit=1)

    assert result['features'][0]['id'] == 0


def test_non_spatial(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 2, 'offset': 0, 'sortby': ['ID_ASC']},
                  {'data': {'things': [{'id': 1, 'name': 'a'},
                                       {'id': 2, 'name': 'b'}]}})
    result = p.query(limit=2)

    assert result['features'][0]['geometry'] is None
    assert result['features'][0]['properties'] == {'id': 1, 'name': 'a'}


def test_skip_geometry(config, transport):
    result = GraphQLProvider(config).query(limit=5, skip_geometry=True)

    assert result['features'][0]['geometry'] is None
    document = transport.calls[-1]['payload']['query']
    assert 'longitude' not in document


def test_select_properties(config, transport):
    p = GraphQLProvider(config)
    result = p.query(limit=5, select_properties=['profileId'])

    assert list(result['features'][0]['properties']) == ['profileId']
    document = transport.calls[-1]['payload']['query']
    assert 'profileCode' not in document
    assert 'longitude' in document


def test_sortby(config, transport):
    result = GraphQLProvider(config).query(
        limit=5, sortby=[{'property': 'countryName', 'order': '-'}])

    assert result['numberReturned'] == 5
    assert data_calls(transport)[-1]['sortby'] == ['COUNTRY_NAME_DESC',
                                                   'PROFILE_ID_ASC']


def test_sortby_tiebreak(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 2, 'offset': 0, 'sortby': ['ID_DESC']},
                  {'data': {'things': [{'id': 2, 'name': 'b'},
                                       {'id': 1, 'name': 'a'}]}})
    result = p.query(limit=2, sortby=[{'property': 'id', 'order': '-'}])

    assert data_calls(transport) == [
        {'limit': 2, 'offset': 0, 'sortby': ['ID_DESC']}]
    assert result['numberReturned'] == 2


def test_sortby_multi_key_ascending(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 2, 'offset': 0,
                   'sortby': ['NAME_ASC', 'ID_DESC']},
                  {'data': {'things': [{'id': 2, 'name': 'a'},
                                       {'id': 1, 'name': 'b'}]}})
    p.query(limit=2, sortby=[{'property': 'name', 'order': '+'},
                             {'property': 'id', 'order': '-'}])

    assert data_calls(transport)[-1]['sortby'] == ['NAME_ASC', 'ID_DESC']


def test_select_properties_non_id(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection(['name'], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 1, 'offset': 0, 'sortby': ['ID_ASC']},
                  {'data': {'things': [{'id': 7, 'name': 'a'}]}})
    result = p.query(limit=1, select_properties=['name'])

    assert 'id' in document
    assert result['features'][0]['id'] == 7
    assert result['features'][0]['properties'] == {'name': 'a'}


def test_sortby_unsortable_400(config):
    p = GraphQLProvider(config)

    with pytest.raises(ProviderInvalidQueryError) as err:
        p.query(sortby=[{'property': 'unknownProp', 'order': '+'}])

    assert err.value.user_msg == 'property is not sortable'


def test_no_id_sort_warns(transport, things_config, caplog):
    synthetic(transport, '''
        enum ThingsOrderBy { NAME_ASC NAME_DESC }
        type Query {
          things(first: Int, offset: Int, orderBy: [ThingsOrderBy!]): [Thing]
        }
        type Thing { id: Int name: String }
    ''')
    with caplog.at_level(logging.WARNING):
        p = GraphQLProvider(things_config)

    assert 'paging order is not guaranteed' in caplog.text
    document = p._document(p._selection([], False)[1], with_sort=False)
    transport.add(SYNTH_URL, document, {'limit': 10, 'offset': 0},
                  {'data': {'things': []}})
    p.query()

    assert 'sortby' not in data_calls(transport)[-1]


@pytest.mark.parametrize('kwargs,error', [
    ({'resulttype': 'hits'}, 'resulttype hits'),
    ({'bbox': [0, 0, 1, 1]}, 'bbox'),
    ({'datetime_': '2020-01-01'}, 'datetime'),
    ({'properties': [('countryName', 'Portugal')]}, 'property filters'),
    ({'q': 'soil'}, 'q is not supported'),
    ({'filterq': "countryName = 'x'"}, 'CQL')
])
def test_unsupported_params_400(config, kwargs, error):
    p = GraphQLProvider(config)

    with pytest.raises(ProviderInvalidQueryError, match=error):
        p.query(**kwargs)


def test_offset_beyond_int32_400(config):
    p = GraphQLProvider(config)

    with pytest.raises(ProviderInvalidQueryError) as err:
        p.query(offset=2 ** 31 - 5, limit=10)

    assert err.value.user_msg == 'offset too large'


def test_upstream_error_500(config, transport):
    p = GraphQLProvider(config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(WOSIS_URL, document,
                  {'limit': 10, 'offset': 0, 'sortby': ['PROFILE_ID_ASC']},
                  {'errors': [{'message': 'statement timeout'}]})

    with pytest.raises(ProviderQueryError, match='GraphQL errors'):
        p.query()


def test_query_reply_not_list(config, transport):
    p = GraphQLProvider(config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(WOSIS_URL, document,
                  {'limit': 10, 'offset': 0, 'sortby': ['PROFILE_ID_ASC']},
                  {'data': {'wosisLatestProfiles': 'nope'}})

    with pytest.raises(ProviderQueryError, match='reply shape'):
        p.query()


def test_id_missing_500(transport, things_config):
    synthetic(transport, THINGS_SDL)
    p = GraphQLProvider(things_config)
    document = p._document(p._selection([], False)[1], with_sort=True)
    transport.add(SYNTH_URL, document,
                  {'limit': 1, 'offset': 0, 'sortby': ['ID_ASC']},
                  {'data': {'things': [{'id': None, 'name': 'a'}]}})

    with pytest.raises(ProviderQueryError, match='id missing'):
        p.query(limit=1)


def test_timeout_connection_error(config, transport):
    p = GraphQLProvider(config)
    failing = mock.patch(
        'pygeoapi.provider.graphql_.requests.Session.post',
        side_effect=requests.Timeout('upstream too slow'))

    with failing, pytest.raises(ProviderConnectionError):
        p.query()


def test_call_headers_timeout(config, transport):
    config['graphql']['headers'] = {'x-api-key': 'k'}
    config['graphql']['timeout'] = 7
    GraphQLProvider(config).query()
    call = transport.calls[-1]

    assert call['kwargs']['timeout'] == 7
    assert call['kwargs']['headers'] == {'x-api-key': 'k'}
    assert call['kwargs']['allow_redirects'] is False


def test_crs_transform(config):
    p = GraphQLProvider(config)
    source = 'http://www.opengis.net/def/crs/OGC/1.3/CRS84'
    target = 'http://www.opengis.net/def/crs/EPSG/0/3857'
    spec = CrsTransformSpec(
        source_crs_uri=source,
        source_crs_wkt=get_crs(source).to_wkt(),
        target_crs_uri=target,
        target_crs_wkt=get_crs(target).to_wkt(),
    )
    plain = p.query()
    transformed = p.query(crs_transform_spec=spec)

    lon, lat = plain['features'][0]['geometry']['coordinates']
    transformer = pyproj.Transformer.from_crs(4326, 3857, always_xy=True)
    expected = transformer.transform(lon, lat)
    coordinates = transformed['features'][0]['geometry']['coordinates']
    assert coordinates == pytest.approx(expected)


def test_document_validates(config, transport):
    GraphQLProvider(config).query()
    document = transport.calls[-1]['payload']['query']
    with open(get_test_file_path('data/graphql/wosis_schema.json')) as fh:
        schema = build_client_schema(json.load(fh)['data'])

    assert parse(document)
    assert validate(schema, parse(document)) == []


def test_api_queryables(graphql_api):
    req = mock_api_request({'f': 'json'})
    headers, code, response = get_collection_queryables(
        graphql_api, req, 'wosis-profiles')

    assert code == HTTPStatus.OK
    queryables = json.loads(response)
    assert 'countryName' in queryables['properties']
    assert 'profileId' in queryables['properties']


def test_api_openapi(graphql_config):
    oas = get_oas(graphql_config)

    assert '/collections/wosis-profiles/items' in oas['paths']
