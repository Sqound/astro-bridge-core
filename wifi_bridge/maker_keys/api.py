"""Administrator metadata/actions; credentials have no read or export route."""
import copy
import re

from ..api import ApiError
from .rsa import Unusable
from .store import StorageUnavailable


class MakerKeyAPI:
    def __init__(self, api): self.api = api

    def handle(self, method, path, query, data, expected):
        if query: raise ApiError(400,'INVALID_FIELDS','Unknown or read-only fields in request.')
        service = self.api.controller.maker_keys
        with self.api.controller.lock:
            try:
                if method == 'GET' and path == '/maker-keys':
                    return 200,dict(service.status(),config_revision=self.api.revision())
                match = re.fullmatch('/maker-keys/operations/([0-9a-f]{32})',path)
                if method == 'GET' and match:
                    if match[1] not in service.jobs: raise ApiError(404,'NOT_FOUND','Unknown API resource.')
                    return 200,dict(operation=copy.deepcopy(service.jobs[match[1]]),config_revision=self.api.revision())
                if path == '/maker-keys/seestar/fetch' and method == 'POST':
                    self.api.fields(data,set())
                    job = service.fetch('seestar')
                    return 202,dict(operation=job,config_revision=self.api.revision())
                if path == '/maker-keys/seestar' and method == 'PUT':
                    self.api.fields(data,{'pem'})
                    service.import_key('seestar',data.get('pem'))
                    return 200,dict(service.status(),config_revision=self.api.revision())
                if path == '/maker-keys/seestar' and method == 'DELETE':
                    self.api.fields(data,set())
                    self.api.controller.protection.precondition(expected,self.api.revision)
                    service.cancel();service.store.verdict('seestar','absent')
                    return 200,dict(service.status(),config_revision=self.api.revision())
            except Unusable:
                raise ApiError(422,'KEY_UNUSABLE','This key file cannot be used.') from None
            except StorageUnavailable:
                raise ApiError(503,'STORAGE_UNAVAILABLE','Encrypted key storage is unavailable on this bridge.') from None
        raise ApiError(404,'NOT_FOUND','Unknown API resource.')
