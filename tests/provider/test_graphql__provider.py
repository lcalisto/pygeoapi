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

from copy import deepcopy

import pytest

from pygeoapi.plugin import load_plugin
from pygeoapi.provider.base import ProviderGenericError
from pygeoapi.provider.graphql_ import GraphQLProvider


@pytest.fixture()
def config():
    # WoSIS, ISRIC World Soil Information
    # URL: https://graphql.isric.org/wosis/graphql
    return {
        'name': 'GraphQL',
        'type': 'feature',
        'data': 'https://graphql.isric.org/wosis/graphql',
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
    expected = '<GraphQLProvider> https://graphql.isric.org/wosis/graphql'
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
    assert p.fields == {}


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
    'graphql.isric.org/wosis/graphql',
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


def test_field_mapping_parsed(config):
    config['graphql']['field_mapping'] = {'country': 'countryName'}
    p = GraphQLProvider(config)

    assert p.field_mapping == {'country': 'countryName'}


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
