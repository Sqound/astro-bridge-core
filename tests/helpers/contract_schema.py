"""Bounded JSON Schema subset used by executable native contract fixtures."""
import re


def validate(value, schema, spec, path='$', depth=0):
    if depth > 40:
        raise AssertionError(path + ': schema nesting limit')
    if '$ref' in schema:
        reference = schema['$ref']
        assert reference.startswith('#/components/schemas/'), path + ': external reference'
        return validate(value, spec['components']['schemas'][reference.rsplit('/', 1)[1]], spec, path, depth + 1)
    for keyword in ('oneOf', 'anyOf'):
        if keyword in schema:
            matches = 0
            for branch in schema[keyword]:
                try:
                    validate(value, branch, spec, path, depth + 1)
                    matches += 1
                except AssertionError:
                    pass
            assert matches == 1 if keyword == 'oneOf' else matches >= 1, path + ': ' + keyword
    if 'const' in schema:
        assert value == schema['const'], path + ': const'
    if 'enum' in schema:
        assert value in schema['enum'], path + ': enum'
    if 'type' in schema:
        kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        matchers = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
                    'string': lambda v: isinstance(v, str), 'integer': lambda v: type(v) is int,
                    'number': lambda v: type(v) in (int, float), 'boolean': lambda v: type(v) is bool,
                    'null': lambda v: v is None}
        assert any(matchers[k](value) for k in kinds), path + ': type'
    if isinstance(value, dict):
        assert len(value) <= 1024, path + ': object bound'
        assert all(key in value for key in schema.get('required', [])), path + ': required'
        for key, item in value.items():
            props = schema.get('properties', {})
            extra = schema.get('additionalProperties', True)
            assert key in props or extra is not False, path + ': unexpected property ' + key
            child = props.get(key, extra if isinstance(extra, dict) else {})
            validate(item, child, spec, path + '.' + key, depth + 1)
    elif isinstance(value, list):
        assert len(value) <= min(1024, schema.get('maxItems', 1024)), path + ': maxItems'
        assert len(value) >= schema.get('minItems', 0), path + ': minItems'
        for index, item in enumerate(value):
            validate(item, schema.get('items', {}), spec, path + '[' + str(index) + ']', depth + 1)
    elif isinstance(value, str):
        assert len(value) <= min(32768, schema.get('maxLength', 32768)), path + ': maxLength'
        assert len(value) >= schema.get('minLength', 0), path + ': minLength'
        if 'pattern' in schema:
            assert re.search(schema['pattern'], value), path + ': pattern'
    elif type(value) in (int, float):
        assert value >= schema.get('minimum', float('-inf')), path + ': minimum'
        assert value <= schema.get('maximum', float('inf')), path + ': maximum'
