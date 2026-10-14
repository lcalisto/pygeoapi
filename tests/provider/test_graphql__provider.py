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
from copy import deepcopy
from http import HTTPStatus
from unittest import mock

import pytest
import requests
from graphql import build_schema, get_introspection_query, graphql_sync

from pygeoapi.api import API
from pygeoapi.api.itemtypes import get_collection_queryables
from pygeoapi.openapi import get_oas
from pygeoapi.plugin import load_plugin
from pygeoapi.provider import graphql_
from pygeoapi.provider.base import (ProviderConnectionError,
                                    ProviderGenericError, ProviderQueryError)
from pygeoapi.provider.graphql_ import GraphQLProvider
from pygeoapi.util import yaml_load

from ..util import get_test_file_path, mock_api_request

WOSIS_URL = 'https://wosis.example.org/graphql'
SYNTH_URL = 'https://graphql.example.org/graphql'
INTROSPECTION = get_introspection_query(descriptions=False)


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
    return fake


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
    # schema recorded from https://graphql.isric.org/wosis/graphql
    # (see tests/data/README.md); tests never touch the network
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
    'wosis.example.org/graphql',
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


@pytest.mark.parametrize('collection', ['wosis profiles', 'a.b', '1abc', ''])
def test_collection_invalid_name(config, collection):
    config['graphql']['collection'] = collection

    with pytest.raises(ProviderGenericError, match='collection'):
        GraphQLProvider(config)


@pytest.mark.parametrize('geometry', [
    'geom',
    {},
    {'x_field': 'longitude'},
    {'x_field': 'longitude', 'y_field': 'latitude', 'geojson_field': 'geom'}
])
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


@pytest.mark.parametrize('bbox_filter', [
    'geom',
    {'field': 'geom'},
    {'field': 'geom', 'operator': 'intersects', 'extra': 1}
])
def test_bbox_filter_invalid(config, bbox_filter):
    config['graphql']['bbox_filter'] = bbox_filter

    with pytest.raises(ProviderGenericError, match='bbox_filter'):
        GraphQLProvider(config)


@pytest.mark.parametrize('key', ['page_size', 'timeout'])
@pytest.mark.parametrize('value', [0, -1, 'many', 2.5, True])
def test_positive_int_keys(config, key, value):
    config['graphql'][key] = value

    with pytest.raises(ProviderGenericError, match=key):
        GraphQLProvider(config)


@pytest.mark.parametrize('mapping', [
    'countryName',
    {},
    {'country': 'country name'},
    {'country': 'a..b'},
    {5: 'countryName'}
])
def test_field_mapping_invalid(config, mapping):
    config['graphql']['field_mapping'] = mapping

    with pytest.raises(ProviderGenericError, match='field_mapping'):
        GraphQLProvider(config)


@pytest.mark.parametrize('headers', [
    'token',
    {'x-api-key': {}},
    {'x-api-key': ['a']},
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


def test_field_mapping_parsed(config):
    config['graphql']['field_mapping'] = {
        'profileId': 'profileId',
        'profileCode': 'profileCode',
        'country': 'countryName'
    }
    p = GraphQLProvider(config)

    assert p.field_mapping == {'profileId': 'profileId',
                               'profileCode': 'profileCode',
                               'country': 'countryName'}


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
    ({'field': 'countryName', 'operator': 'bboxIntersects2D'}, 'operator'),
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
