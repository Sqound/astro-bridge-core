"""Conservative additive-only guard for the frozen native OpenAPI surface.

This is a reviewed subset, not a general JSON Schema implication solver. Unknown
semantic changes fail closed. Descriptions/examples and implementation status are
not wire semantics. Request additions must remain optional; response consumers
must tolerate new properties and enum/discriminated-event values.
"""
import copy


METHODS = {'get', 'post', 'put', 'patch', 'delete', 'options', 'head', 'trace'}
ANNOTATIONS = {'description', 'summary', 'title', 'example', 'examples', '$comment'}


def native_snapshot(spec):
    """Capture native routes and every referenced component, including security."""
    result = {key: copy.deepcopy(spec[key]) for key in ('openapi', 'servers', 'security')}
    result['paths'] = {}
    result['components'] = {'schemas': {}, 'securitySchemes': copy.deepcopy(spec['components']['securitySchemes'])}
    pending = []

    def references(value):
        if isinstance(value, dict):
            if '$ref' in value:
                pending.append(value['$ref'])
            for child in value.values(): references(child)
        elif isinstance(value, list):
            for child in value: references(child)

    for path, item in spec['paths'].items():
        selected = {method: copy.deepcopy(entry) for method, entry in item.items()
                    if method in METHODS and 'native-client' in entry.get('tags', [])}
        if selected:
            selected.update({key: copy.deepcopy(value) for key, value in item.items() if key not in METHODS})
            result['paths'][path] = selected
            references(selected)
    while pending:
        reference = pending.pop()
        assert reference.startswith('#/components/schemas/'), 'Unsupported component reference'
        name = reference.rsplit('/', 1)[1]
        if name not in result['components']['schemas']:
            value = copy.deepcopy(spec['components']['schemas'][name])
            result['components']['schemas'][name] = value
            references(value)
    return result


def assert_additive(before, after):
    """Raise with the exact contract path when an incompatible change is found."""
    visited = set()

    def require(condition, path, message):
        if not condition: raise AssertionError(path + ': ' + message)

    def schema(old, new, direction, path):
        require(isinstance(new, dict), path, 'schema removed or replaced')
        for key in (old.keys() | new.keys()) - ANNOTATIONS:
            location = path + '/' + key
            if key == 'properties':
                for name, value in old.get(key, {}).items():
                    require(name in new.get(key, {}), location, 'property removed: ' + name)
                    schema(value, new[key][name], direction, location + '/' + name)
            elif key == 'required':
                previous, current = set(old.get(key, [])), set(new.get(key, []))
                require(current == previous if direction == 'request' else previous <= current,
                        location, 'new request requirement or removed response guarantee')
            elif key == 'enum':
                require(key in old and key in new and all(value in new[key] for value in old[key]), location, 'enum value removed or constraint introduced')
            elif key in ('oneOf', 'anyOf'):
                require(key in old and key in new, location, 'union kind changed')
                # Matching recursively permits additive fields within a branch.
                unmatched = list(new[key])
                for branch in old[key]:
                    for index, candidate in enumerate(unmatched):
                        try: schema(branch, candidate, direction, location)
                        except AssertionError: continue
                        unmatched.pop(index)
                        break
                    else: raise AssertionError(location + ': union branch removed or changed')
                if key == 'oneOf':
                    # An overlapping branch would invalidate previously accepted values.
                    def discriminator(branch, spec, depth=0):
                        require(depth < 40, location, 'discriminator nesting limit')
                        if '$ref' in branch:
                            branch = spec['components']['schemas'][branch['$ref'].rsplit('/', 1)[1]]
                            return discriminator(branch, spec, depth + 1)
                        for union in ('oneOf', 'anyOf'):
                            if union in branch:
                                values = [discriminator(child, spec, depth + 1) for child in branch[union]]
                                return set().union(*values) if values and all(values) else set()
                        field = branch.get('properties', {}).get('type', {})
                        return set(field.get('enum', [])) if 'type' in branch.get('required', []) else set()
                    discriminators = [discriminator(branch, after) for branch in new[key]]
                    if unmatched or all(discriminators):
                        seen = set()
                        for values in discriminators:
                            require(values and not seen & values, location, 'oneOf branches need disjoint required type discriminators')
                            seen.update(values)
            elif key == '$ref':
                require(old.get(key) == new.get(key) and key in old, location, 'reference changed')
                reference = old[key]
                require(reference.startswith('#/components/schemas/'), location, 'unsupported reference')
                marker = (reference, direction)
                if marker not in visited:
                    name = reference.rsplit('/', 1)[1]
                    require(name in after['components']['schemas'], reference, 'referenced schema removed')
                    # Record only successful comparisons: failed union candidates cannot bypass checks.
                    schema(before['components']['schemas'][name], after['components']['schemas'][name], direction, reference)
                    visited.add(marker)
            elif key in ('items', 'additionalProperties') and isinstance(old.get(key), dict):
                schema(old[key], new.get(key), direction, location)
            elif key == 'x-contract-status':
                require(old.get(key) == new.get(key) or (old.get(key), new.get(key)) == ('planned', 'implemented'), location, 'implementation regressed')
            else:
                require(key in old and key in new and old[key] == new[key], location, 'existing semantics changed')

    def content(old, new, direction, path):
        for media, entry in old.items():
            require(media in new, path, 'media type removed: ' + media)
            schema(entry['schema'], new[media].get('schema'), direction, path + '/' + media)
            for key in (entry.keys() | new[media].keys()) - ANNOTATIONS - {'schema'}:
                require(entry.get(key) == new[media].get(key), path, 'media semantics changed: ' + key)

    def parameters(old, new, path):
        index = {(p['in'], p['name']): p for p in new}
        require(len(index) == len(new), path, 'duplicate parameters')
        for entry in old:
            key = entry['in'], entry['name']
            require(key in index, path, 'parameter removed: ' + str(key))
            current = index.pop(key)
            require(not current.get('required', False) or entry.get('required', False), path, 'parameter became required')
            schema(entry['schema'], current['schema'], 'request', path + '/' + entry['name'])
            for field in (entry.keys() | current.keys()) - ANNOTATIONS - {'required', 'schema'}:
                require(entry.get(field) == current.get(field), path, 'parameter semantics changed: ' + field)
        require(all(not p.get('required', False) for p in index.values()), path, 'new required parameter')

    require(before['openapi'] == after['openapi'], '#/openapi', 'specification dialect changed')
    require(before['security'] == after.get('security'), '#/security', 'authentication changed')
    for server in before['servers']:
        require(server in after.get('servers', []), '#/servers', 'transport removed or changed')
    for name, scheme in before['components']['securitySchemes'].items():
        require(scheme == after['components']['securitySchemes'].get(name), '#/components/securitySchemes/' + name, 'security scheme changed')
    for path, item in before['paths'].items():
        require(path in after['paths'], path, 'route removed')
        current_item = after['paths'][path]
        parameters(item.get('parameters', []), current_item.get('parameters', []), path)
        for key in (item.keys() | current_item.keys()) - METHODS - ANNOTATIONS - {'parameters'}:
            require(item.get(key) == current_item.get(key), path, 'path semantics changed: ' + key)
        for method, entry in item.items():
            if method not in METHODS: continue
            location = method.upper() + ' ' + path
            require(method in current_item, location, 'method removed')
            current = current_item[method]
            parameters(entry.get('parameters', []), current.get('parameters', []), location)
            for key in (entry.keys() | current.keys()) - ANNOTATIONS - {'parameters', 'requestBody', 'responses'}:
                if key == 'tags':
                    require(set(entry[key]) <= set(current.get(key, [])), location, 'contract tag removed')
                elif key == 'x-contract-status':
                    require(entry[key] == current.get(key) or (entry[key], current.get(key)) == ('planned', 'implemented'), location, 'implementation regressed')
                else:
                    require(entry.get(key) == current.get(key), location, 'operation semantics changed: ' + key)
            if 'requestBody' in entry:
                require('requestBody' in current, location, 'request body removed')
                old_body, new_body = entry['requestBody'], current['requestBody']
                require(not new_body.get('required') or old_body.get('required'), location, 'body became required')
                content(old_body['content'], new_body['content'], 'request', location + '/requestBody')
                for key in (old_body.keys() | new_body.keys()) - ANNOTATIONS - {'required', 'content'}:
                    require(old_body.get(key) == new_body.get(key), location, 'body semantics changed: ' + key)
            elif 'requestBody' in current:
                require(not current['requestBody'].get('required'), location, 'new required body')
            for code, response in entry['responses'].items():
                require(code in current.get('responses', {}), location, 'response removed: ' + code)
                target = current['responses'][code]
                content(response.get('content', {}), target.get('content', {}), 'response', location + '/responses/' + code)
                for key in (response.keys() | target.keys()) - ANNOTATIONS - {'content'}:
                    require(response.get(key) == target.get(key), location, 'response semantics changed: ' + key)
