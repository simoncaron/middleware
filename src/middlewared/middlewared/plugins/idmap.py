import enum
import asyncio
import errno
import datetime
import wbclient

from middlewared.schema import accepts, Bool, Dict, Int, Password, Patch, Ref, Str, LDAP_DN, OROperator
from middlewared.service import CallError, CRUDService, job, private, ValidationErrors, filterable
from middlewared.service_exception import MatchNotFound
from middlewared.plugins.directoryservices import SSL
from middlewared.plugins.idmap_.utils import IDType, TRUENAS_IDMAP_MAX, WBClient, WBCErr
import middlewared.sqlalchemy as sa
from middlewared.utils import run, filter_list
from middlewared.validators import Range
from middlewared.plugins.smb import SMBPath


SID_LOCAL_USER_PREFIX = "S-1-22-1-"
SID_LOCAL_GROUP_PREFIX = "S-1-22-2-"

"""
See MS-DTYP 2.4.2.4

Most of these groups will never be used on production servers.
We are statically assigning IDs (based on idmap low range)
to cover edge cases where users may have decided to copy data
from a local Windows server share (for example) and preserve
the existing Security Descriptor. We want the mapping to be
consistent so that ZFS replication of TrueNAS server A to
TrueNAS server B will result in same effective permissions
for users and groups with no unexpected elevation of permissions.

Entries may be appended to this list. Ordering is used to determine
the GID assigned to the builtin.
Once a new entry has been appended, the corresponding padding
in smb/_groupmap.py should be decreased
"""
WELL_KNOWN_SIDS = [
    {"name": "NULL", "sid": "S-1-0-0", "set": False},
    {"name": "EVERYONE", "sid": "S-1-1-0", "set": True},
    {"name": "LOCAL", "sid": "S-1-2-0", "set": True},
    {"name": "CONSOLE_LOGON", "sid": "S-1-2-1", "set": True},
    {"name": "CREATOR_OWNER", "sid": "S-1-3-0", "set": True},
    {"name": "CREATOR_GROUP", "sid": "S-1-3-1", "set": True},
    {"name": "OWNER_RIGHTS", "sid": "S-1-3-4", "set": True},
    {"name": "DIALUP", "sid": "S-1-5-1", "set": True},
    {"name": "NETWORK", "sid": "S-1-5-2", "set": True},
    {"name": "BATCH", "sid": "S-1-5-3", "set": True},
    {"name": "INTERACTIVE", "sid": "S-1-5-4", "set": True},
    {"name": "SERVICE", "sid": "S-1-5-6", "set": True},
    {"name": "ANONYMOUS", "sid": "S-1-5-7", "set": True},
    {"name": "AUTHENTICATED_USERS", "sid": "S-1-5-11", "set": True},
    {"name": "TERMINAL_SERVER_USER", "sid": "S-1-5-13", "set": True},
    {"name": "REMOTE_AUTHENTICATED_LOGON", "sid": "S-1-5-14", "set": True},
    {"name": "LOCAL_SYSTEM", "sid": "S-1-5-18", "set": True},
    {"name": "LOCAL_SERVICE", "sid": "S-1-5-19", "set": True},
    {"name": "NETWORK_SERVICE", "sid": "S-1-5-20", "set": True},
]


class DSType(enum.Enum):
    """
    The below DS_TYPES are defined for use as system domains for idmap backends.
    DS_TYPE_NT4 is defined, but has been deprecated. DS_TYPE_DEFAULT_DOMAIN corresponds
    with the idmap settings under services->SMB, and is represented by 'idmap domain = *'
    in the smb4.conf. The only instance where the idmap backend for the default domain will
    not be 'tdb' is when the server is (1) joined to active directory and (2) autorid is enabled.
    """
    DS_TYPE_ACTIVEDIRECTORY = 1
    DS_TYPE_LDAP = 2
    DS_TYPE_NIS = 3
    DS_TYPE_FREEIPA = 4
    DS_TYPE_DEFAULT_DOMAIN = 5

    def choices():
        return [x.name for x in DSType]


class IdmapBackend(enum.Enum):
    AD = {
        'description': 'The AD backend provides a way for TrueNAS to read id '
                       'mappings from an Active Directory server that uses '
                       'RFC2307/SFU schema extensions. ',
        'parameters': {
            'schema_mode': {"required": False, "default": 'RFC2307'},
            'unix_primary_group': {"required": False, "default": False},
            'unix_nss_info': {"required": False, "default": False},
        },
        'has_secrets': False,
        'services': ['AD'],
    }
    AUTORID = {
        'description': 'Similar to the RID backend, but automatically configures '
                       'the range to be used for each domain, so that there is no '
                       'need to specify a specific range for each domain in the forest '
                       'The only needed configuration is the range of UID/GIDs to use '
                       'for user/group mappings and an optional size for the ranges.',
        'parameters': {
            'rangesize': {"required": False, "default": 100000},
            'readonly': {"required": False, "default": False},
            'ignore_builtin': {"required": False, "default": False},
        },
        'has_secrets': False,
        'services': ['AD'],
    }
    LDAP = {
        'description': 'Stores and retrieves mapping tables in an LDAP directory '
                       'service. Default for LDAP directory service.',
        'parameters': {
            'ldap_base_dn': {"required": True, "default": None},
            'ldap_user_dn': {"required": True, "default": None},
            'ldap_url': {"required": True, "default": None},
            'ldap_user_dn_password': {"required": False, "default": None},
            'ssl': {"required": False, "default": SSL.NOSSL.value},
            'validate_certificates': {"required": False, "default": True},
            'readonly': {"required": False, "default": False},
        },
        'has_secrets': True,
        'services': ['AD', 'LDAP'],
    }
    NSS = {
        'description': 'Provides a simple means of ensuring that the SID for a '
                       'Unix user is reported as the one assigned to the '
                       'corresponding domain user.',
        'parameters': {
            'linked_service': {"required": False, "default": "LOCAL_ACCOUNT"},
        },
        'has_secrets': False,
        'services': ['AD', 'LDAP'],
    }
    RFC2307 = {
        'description': 'Looks up IDs in the Active Directory LDAP server '
                       'or an extenal (non-AD) LDAP server. IDs must be stored '
                       'in RFC2307 ldap schema extensions. Other schema extensions '
                       'such as Services For Unix (SFU20/SFU30) are not supported.',
        'parameters': {
            'ldap_server': {"required": False, "default": "AD"},
            'bind_path_user': {"required": False, "default": None},
            'bind_path_group': {"required": False, "default": None},
            'user_cn': {"required": False, "default": None},
            'cn_realm': {"required": False, "default": None},
            'ldap_domain': {"required": False, "default": None},
            'ldap_url': {"required": False, "default": None},
            'ldap_user_dn': {"required": True, "default": None},
            'ldap_user_dn_password': {"required": False, "default": None},
            'ldap_realm': {"required": False, "default": None},
            'validate_certificates': {"required": False, "default": True},
            'ssl': {"required": False, "default": SSL.NOSSL.value},
        },
        'has_secrets': True,
        'services': ['AD', 'LDAP'],
    }
    RID = {
        'description': 'Default for Active Directory service. requires '
                       'an explicit configuration for each domain, using '
                       'disjoint ranges.',
        'parameters': {
            'sssd_compat': {"required": False, "default": False},
        },
        'has_secrets': False,
        'services': ['AD'],
    }
    TDB = {
        'description': 'Default backend used to store mapping tables for '
                       'BUILTIN and well-known SIDs.',
        'parameters': {
            'readonly': {"required": False, "default": False},
        },
        'services': ['AD'],
    }

    def supported_keys(self):
        return [str(x) for x in self.value['parameters'].keys()]

    def required_keys(self):
        ret = []
        for k, v in self.value['parameters'].items():
            if v['required']:
                ret.append(str(k))
        return ret

    def defaults(self):
        ret = {}
        for k, v in self.value['parameters'].items():
            if v['default'] is not None:
                ret.update({k: v['default']})
        return ret

    def ds_choices():
        directory_services = ['AD', 'LDAP']
        ret = {}
        for ds in directory_services:
            ret[ds] = []

        ds = {'AD': [], 'LDAP': []}
        for x in IdmapBackend:
            for ds in directory_services:
                if ds in x.value['services']:
                    ret[ds].append(x.name)

        return ret

    def stores_secret(self):
        return self.value['has_secrets']


class IdmapDomainModel(sa.Model):
    __tablename__ = 'directoryservice_idmap_domain'

    id = sa.Column(sa.Integer(), primary_key=True)
    idmap_domain_name = sa.Column(sa.String(120), unique=True)
    idmap_domain_dns_domain_name = sa.Column(sa.String(255), nullable=True, unique=True)
    idmap_domain_range_low = sa.Column(sa.Integer())
    idmap_domain_range_high = sa.Column(sa.Integer())
    idmap_domain_idmap_backend = sa.Column(sa.String(120), default='rid')
    idmap_domain_options = sa.Column(sa.JSON(dict))
    idmap_domain_certificate_id = sa.Column(sa.ForeignKey('system_certificate.id'), index=True, nullable=True)


class IdmapDomainService(CRUDService):

    ENTRY = Patch(
        'idmap_domain_create', 'idmap_domain_entry',
        ('add', Int('id')),
    )

    class Config:
        datastore = 'directoryservice.idmap_domain'
        datastore_prefix = 'idmap_domain_'
        namespace = 'idmap'
        datastore_extend = 'idmap.idmap_extend'
        cli_namespace = 'directory_service.idmap'


    def __wbclient_ctx(self, retry=True):
        """
        Wrapper around setting up a temporary winbindd client context
        If winbindd is stopped, then try to once to start it and if that
        fails, present reason to caller.
        """
        try:
            return WBClient()
        except wbclient.WBCError as e:
            if not retry or e.error_code != wbclient.WBC_ERR_WINBIND_NOT_AVAILABLE:
                raise e

        if not self.middleware.call_sync('systemdataset.sysdataset_path'):
            raise CallError(
                'Unexpected filesystem mounted in the system dataset path. '
                'This may indicate a failure to initialize the system dataset '
                'and may be resolved by reviewing and fixing errors in the system '
                'dataset configuration.', errno.EAGAIN
            )

        self.middleware.call_sync('service.start', 'idmap', {'silent': False})
        return self.__wbclient_ctx(False)

    @private
    async def idmap_extend(self, data):
        if data.get('idmap_backend'):
            data['idmap_backend'] = data['idmap_backend'].upper()

        opt_enums = ['ssl', 'linked_service']
        if data.get('options'):
            for i in opt_enums:
                if data['options'].get(i):
                    data['options'][i] = data['options'][i].upper()

        return data

    @private
    async def idmap_compress(self, data):
        opt_enums = ['ssl', 'linked_service']
        if data.get('options'):
            for i in opt_enums:
                if data['options'].get(i):
                    data['options'][i] = data['options'][i].lower()

        data['idmap_backend'] = data['idmap_backend'].lower()

        return data

    @private
    async def get_next_idmap_range(self):
        """
        Increment next high range by 100,000,000 ids. This number has
        to accomodate the highest available rid value for a domain.
        Configured idmap ranges _must_ not overlap.
        """
        domains = await self.query()
        sorted_idmaps = sorted(domains, key=lambda domain: domain['range_high'])
        low_range = sorted_idmaps[-1]['range_high'] + 1
        high_range = sorted_idmaps[-1]['range_high'] + 100000000
        return (low_range, high_range)

    @private
    async def snapshot_samba4_dataset(self):
        sysdataset = (await self.middleware.call('systemdataset.config'))['basename']
        ts = str(datetime.datetime.now(datetime.timezone.utc).timestamp())[:10]
        await self.middleware.call('zfs.snapshot.create', {'dataset': f'{sysdataset}/samba4',
                                                           'name': f'wbc-{ts}'})

    @private
    @filterable
    def known_domains(self, query_filters, query_options):
        try:
            entries = [entry.domain_info() for entry in WBClient().all_domains()]
        except wbclient.WBCError as e:
            match e.error_code:
                case wbclient.WBC_ERR_INVALID_RESPONSE:
                    # Our idmap domain is not AD and so this is not expected to succeed
                    return []
                case wbclient.WBC_ERR_WINBIND_NOT_AVAILABLE:
                    # winbindd process is stopped this may be in hot code path. Skip
                    return []
                case _:
                    raise

        return filter_list(entries, query_filters, query_options)

    @private
    @filterable
    def online_status(self, query_filters, query_options):
        try:
            all_info = self.known_domains()
        except wbclient.WBCError as e:
            raise CallError(str(e), WBCErr[e.error_code], e.error_code)

        entries = [{
            'domain': dom_info['netbios_domain'],
            'online': dom_info['online']
        } for dom_info in all_info]

        return filter_list(entries, query_filters, query_options)

    @private
    def domain_info(self, domain):
        if domain == 'DS_TYPE_ACTIVEDIRECTORY':
            return WBClient().domain_info()

        elif domain == 'DS_TYPE_DEFAULT_DOMAIN':
            return WBClient().domain_info('BUILTIN')

        elif domain == 'DS_TYPE_LDAP':
            return None

        return WBClient().domain_info(domain)

    @private
    async def get_sssd_low_range(self, domain, sssd_config=None, seed=0xdeadbeef):
        """
        This is best effort attempt for SSSD compatibility. It will allocate low
        range for then initial slice in the SSSD environment. The SSSD allocation algorithm
        is non-deterministic. Domain SID string is converted to a 32-bit hashed value
        using murmurhash3 algorithm.

        The modulus of this value with the total number of available slices is used to
        pick the slice. This slice number is then used to calculate the low range for
        RID 0. With the default settings in SSSD this will be deterministic as long as
        the domain has less than 200,000 RIDs.
        """
        sid = (await self.middleware.call('idmap.domain_info', domain))['sid']
        sssd_config = {} if not sssd_config else sssd_config
        range_size = sssd_config.get('range_size', 200000)
        range_low = sssd_config.get('range_low', 10001)
        range_max = sssd_config.get('range_max', 2000200000)
        max_slices = int((range_max - range_low) / range_size)

        data = bytearray(sid.encode())
        datalen = len(data)
        hash_ = seed
        data_bytes = data

        c1 = 0xcc9e2d51
        c2 = 0x1b873593
        r1 = 15
        r2 = 13
        n = 0xe6546b64

        while datalen >= 4:
            k = int.from_bytes(data_bytes[:4], byteorder='little') & 0xFFFFFFFF
            data_bytes = data_bytes[4:]
            datalen = datalen - 4
            k = (k * c1) & 0xFFFFFFFF
            k = (k << r1 | k >> 32 - r1) & 0xFFFFFFFF
            k = (k * c2) & 0xFFFFFFFF
            hash_ ^= k
            hash_ = (hash_ << r2 | hash_ >> 32 - r2) & 0xFFFFFFFF
            hash_ = (hash_ * 5 + n) & 0xFFFFFFFF

        if datalen > 0:
            k = 0
            if datalen >= 3:
                k = k | data_bytes[2] << 16
            if datalen >= 2:
                k = k | data_bytes[1] << 8
            if datalen >= 1:
                k = k | data_bytes[0]
                k = (k * c1) & 0xFFFFFFFF
                k = (k << r1 | k >> 32 - r1) & 0xFFFFFFFF
                k = (k * c2) & 0xFFFFFFFF
                hash_ ^= k

        hash_ = (hash_ ^ len(data)) & 0xFFFFFFFF
        hash_ ^= hash_ >> 16
        hash_ = (hash_ * 0x85ebca6b) & 0xFFFFFFFF
        hash_ ^= hash_ >> 13
        hash_ = (hash_ * 0xc2b2ae35) & 0xFFFFFFFF
        hash_ ^= hash_ >> 16

        return (hash_ % max_slices) * range_size + range_size

    @private
    async def flush_gencache(self):
        gencache_flush = await run(['net', 'cache', 'flush'], check=False)
        if gencache_flush.returncode != 0:
            raise CallError(f'Attempt to flush gencache failed with error: {gencache_flush.stderr.decode().strip()}')

    @accepts()
    @job(lock='clear_idmap_cache', lock_queue_size=1)
    async def clear_idmap_cache(self, job):
        """
        Stop samba, remove the winbindd_cache.tdb file, start samba, flush samba's cache.
        This should be performed after finalizing idmap changes.
        """
        smb_started = await self.middleware.call('service.started', 'cifs')
        await self.middleware.call('service.stop', 'idmap')

        try:
            await self.middleware.call('tdb.wipe', {
                'name': f'{SMBPath.CACHE_DIR.platform()}/winbindd_cache.tdb',
                'tdb-options': {'data_type': 'STRING', 'backend': 'CUSTOM'}
            })

        except FileNotFoundError:
            self.logger.debug("Failed to remove winbindd_cache.tdb. File not found.")

        except Exception:
            self.logger.debug("Failed to remove winbindd_cache.tdb.", exc_info=True)

        await self.flush_gencache()

        await self.middleware.call('service.start', 'idmap')
        if smb_started:
            await self.middleware.call('service.start', 'cifs')

    @private
    async def may_enable_trusted_domains(self):
        domains = await self.query([['name', '!=', 'DS_TYPE_DEFAULT_DOMAIN'], ['name', '!=', 'DS_TYPE_LDAP']])
        primary = filter_list(domains, [['name', '=', 'DS_TYPE_ACTIVEDIRECTORY']], {'get': True})

        if primary['idmap_backend'] == IdmapBackend.AUTORID.name or len(domains) > 1:
            return True

        return False

    @accepts(roles=['READONLY'])
    async def backend_options(self):
        """
        This returns full information about idmap backend options. Not all
        `options` are valid for every backend.
        """
        return {x.name: x.value for x in IdmapBackend}

    @accepts(
        Str('idmap_backend', enum=[x.name for x in IdmapBackend]),
    )
    async def options_choices(self, backend):
        """
        Returns a list of supported keys for the specified idmap backend.
        """
        return IdmapBackend[backend].supported_keys()

    @accepts()
    async def backend_choices(self):
        """
        Returns array of valid idmap backend choices per directory service.
        """
        return IdmapBackend.ds_choices()

    @private
    async def validate(self, schema_name, data, verrors):
        if data['name'] == DSType.DS_TYPE_LDAP.name:
            if data['idmap_backend'] not in (await self.backend_choices())['LDAP']:
                verrors.add(f'{schema_name}.idmap_backend',
                            f'idmap backend [{data["idmap_backend"]}] is not appropriate '
                            f'for the system domain type {data["name"]}')

        elif data['name'] == DSType.DS_TYPE_DEFAULT_DOMAIN.name:
            if data['idmap_backend'] != 'TDB':
                verrors.add(f'{schema_name}.idmap_backend',
                            'TDB is the only supported idmap backend for DS_TYPE_DEFAULT_DOMAIN.')

        if data['range_high'] < data['range_low']:
            """
            If we don't exit at this point further range() operations will raise an IndexError.
            """
            verrors.add(f'{schema_name}.range_low', 'Idmap high range must be greater than idmap low range')
            return

        if data.get('certificate') and not await self.middleware.call(
            'certificate.query', [['id', '=', data['certificate']]]
        ):
            verrors.add(f'{schema_name}.certificate', 'Please specify a valid certificate.')

        configured_domains = await self.query()
        ds_state = await self.middleware.call("directoryservices.get_state")
        ldap_enabled = True if ds_state['ldap'] in ['HEALTHY', 'JOINING'] else False
        ad_enabled = True if ds_state['activedirectory'] in ['HEALTHY', 'JOINING'] else False
        new_range = range(data['range_low'], data['range_high'])
        idmap_backend = data.get('idmap_backend')
        for i in configured_domains:
            # Do not generate validation error comparing to oneself.
            if i['id'] == data.get('id', -1):
                continue

            if i['name'] == data['name']:
                verrors.add(f'{schema_name}.name', 'Name must be unique.')

            # Do not generate validation errors for overlapping with a disabled DS.
            if not ldap_enabled and i['name'] == 'DS_TYPE_LDAP':
                continue

            if not ad_enabled and i['name'] == 'DS_TYPE_ACTIVEDIRECTORY':
                continue

            # Idmap settings under Services->SMB are ignored when autorid is enabled.
            if idmap_backend == IdmapBackend.AUTORID.name and i['name'] == 'DS_TYPE_DEFAULT_DOMAIN':
                continue

            # Overlap between ranges defined for 'ad' backend are permitted.
            if idmap_backend == IdmapBackend.AD.name and i['idmap_backend'] == IdmapBackend.AD.name:
                continue

            existing_range = range(i['range_low'], i['range_high'])
            if range(max(existing_range[0], new_range[0]), min(existing_range[-1], new_range[-1]) + 1):
                verrors.add(f'{schema_name}.range_low',
                            f'new idmap range [{data["range_low"]}-{data["range_high"]}] '
                            'conflicts with existing range for domain '
                            f'[{i["name"]}], range: [{i["range_low"]}-{i["range_high"]}].')

    @private
    async def validate_options(self, schema_name, data, verrors, check=['MISSING', 'EXTRA']):
        supported_keys = set(IdmapBackend[data['idmap_backend']].supported_keys())
        required_keys = set(IdmapBackend[data['idmap_backend']].required_keys())
        provided_keys = set([str(x) for x in data['options'].keys()])

        missing_keys = required_keys - provided_keys
        extra_keys = provided_keys - supported_keys

        if 'MISSING' in check:
            for k in missing_keys:
                verrors.add(f'{schema_name}.options.{k}',
                            f'[{k}] is a required parameter for the [{data["idmap_backend"]}] idmap backend.')

        if 'EXTRA' in check:
            for k in extra_keys:
                verrors.add(f'{schema_name}.options.{k}',
                            f'[{k}] is not a valid parameter for the [{data["idmap_backend"]}] idmap backend.')

    @private
    async def prune_keys(self, data):
        supported_keys = set(IdmapBackend[data['idmap_backend']].supported_keys())
        provided_keys = set([str(x) for x in data['options'].keys()])

        for k in (provided_keys - supported_keys):
            data['options'].pop(k)

    @private
    async def idmap_conf_to_client_config(self, data):
        options = data['options'].copy()
        if data['idmap_backend'] not in ['LDAP', 'RFC2307']:
            raise CallError(f'{data["idmap_backend"]}: invalid idmap backend')

        if data['idmap_backend'] == 'LDAP':
            uri = options["ldap_url"]
            basedn = options["ldap_base_dn"]
        else:
            if data['options']['ldap_server'] == 'AD':
                uri = options["ldap_domain"]
            else:
                uri = options["ldap_url"]

            basedn = options["bind_path_user"]

        credentials = {
            "binddn": options["ldap_user_dn"],
            "bindpw": options["ldap_user_dn_password"],
        }

        security = {
            "ssl": options["ssl"],
            "sasl": "SEAL",
            "validate_certificates": options["validate_certificates"],
        }

        return {
            "uri_list": [f'{"ldaps://" if security["ssl"] == "ON" else "ldap://"}{uri}'],
            "basedn": basedn,
            "bind_type": "PLAIN",
            "credentials": credentials,
            "security": security,
        }

    @filterable
    async def query(self, filters, options):
        extra = options.get("extra", {})
        more_info = extra.get("additional_information", [])
        ret = await super().query(filters, options)
        if 'DOMAIN_INFO' in more_info:
            for entry in ret:
                try:
                    domain_info = await self.middleware.call('idmap.domain_info', entry['name'])
                except wbclient.WBCError as e:
                    if e.error_code != wbclient.WBC_ERR_DOMAIN_NOT_FOUND:
                        self.logger.debug(
                            "Failed to retrieve domain info for domain %s: %s",
                            entry['name'], e
                        )
                    domain_info = None

                entry.update({'domain_info': domain_info})

        return ret

    @accepts(Dict(
        'idmap_domain_create',
        Str('name', required=True),
        Str('dns_domain_name'),
        Int('range_low', required=True, validators=[Range(min_=1000, max_=TRUENAS_IDMAP_MAX)]),
        Int('range_high', required=True, validators=[Range(min_=1000, max_=TRUENAS_IDMAP_MAX)]),
        Str('idmap_backend', required=True, enum=[x.name for x in IdmapBackend]),
        Int('certificate', null=True),
        OROperator(
            Dict(
                'idmap_ad_options',
                Ref('nss_info_ad', 'schema_mode'),
                Bool('unix_primary_group', default=False),
                Bool('unix_nss_info', default=False),
            ),
            Dict(
                'idmap_autorid_options',
                Int('rangesize', default=100000, validators=[Range(min_=10000, max_=1000000000)]),
                Bool('readonly', default=False),
                Bool('ignore_builtin', default=False),
            ),
            Dict(
                'idmap_ldap_options',
                LDAP_DN('ldap_base_dn'),
                LDAP_DN('ldap_user_dn'),
                Password('ldap_user_dn_password'),
                Str('ldap_url'),
                Bool('readonly', default=False),
                Ref('ldap_ssl_choice', 'ssl'),
                Bool('validate_certificates', default=True),
            ),
            Dict(
                'idmap_nss_options',
                Str('linked_service', default='LOCAL_ACCOUNT', enum=['LOCAL_ACCOUNT', 'LDAP']),
            ),
            Dict(
                'idmap_rfc2307_options',
                Str('ldap_server', required=True, enum=['AD', 'STANDALONE']),
                Bool('ldap_realm', default=False),
                LDAP_DN('bind_path_user'),
                LDAP_DN('bind_path_group'),
                Bool('user_cn', default=False),
                Str('cn_realm'),
                Str('ldap_domain'),
                Str('ldap_url'),
                LDAP_DN('ldap_user_dn'),
                Password('ldap_user_dn_password'),
                Ref('ldap_ssl_choice', 'ssl'),
                Bool('validate_certificates', default=True),
            ),
            Dict(
                'idmap_rid_options',
                Bool('sssd_compat', default=False),
            ),
            Dict(
                'idmap_tdb_options',
            ),
            default={},
            name='options',
            title='idmap_options',
        ),
        register=True
    ))
    async def do_create(self, data):
        """
        Create a new IDMAP domain. These domains must be unique. This table
        will be automatically populated after joining an Active Directory domain
        if "allow trusted domains" is set to True in the AD service configuration.
        There are three default system domains: DS_TYPE_ACTIVEDIRECTORY, DS_TYPE_LDAP, DS_TYPE_DEFAULT_DOMAIN.
        The system domains correspond with the idmap settings under Active Directory, LDAP, and SMB
        respectively.

        `name` the pre-windows 2000 domain name.

        `DNS_domain_name` DNS name of the domain.

        `idmap_backend` provides a plugin interface for Winbind to use varying
        backends to store SID/uid/gid mapping tables. The correct setting
        depends on the environment in which the NAS is deployed.

        `range_low` and `range_high` specify the UID and GID range for which this backend is authoritative.

        `certificate_id` references the certificate ID of the SSL certificate to use for certificate-based
        authentication to a remote LDAP server. This parameter is not supported for all idmap backends as some
        backends will generate SID to ID mappings algorithmically without causing network traffic.

        `options` are additional parameters that are backend-dependent:

        `AD` idmap backend options:
        `unix_primary_group` If True, the primary group membership is fetched from the LDAP attributes (gidNumber).
        If False, the primary group membership is calculated via the "primaryGroupID" LDAP attribute.

        `unix_nss_info` if True winbind will retrieve the login shell and home directory from the LDAP attributes.
        If False or if the AD LDAP entry lacks the SFU attributes the smb4.conf parameters `template shell` and `template homedir` are used.

        `schema_mode` Defines the schema that idmap_ad should use when querying Active Directory regarding user and group information.
        This can be either the RFC2307 schema support included in Windows 2003 R2 or the Service for Unix (SFU) schema.
        For SFU 3.0 or 3.5 please choose "SFU", for SFU 2.0 please choose "SFU20". The behavior of primary group membership is
        controlled by the unix_primary_group option.

        `AUTORID` idmap backend options:
        `readonly` sets the module to read-only mode. No new ranges will be allocated and new mappings
        will not be created in the idmap pool.

        `ignore_builtin` ignores mapping requests for the BUILTIN domain.

        `LDAP` idmap backend options:
        `ldap_base_dn` defines the directory base suffix to use for SID/uid/gid mapping entries.

        `ldap_user_dn` defines the user DN to be used for authentication.

        `ldap_url` specifies the LDAP server to use for SID/uid/gid map entries.

        `ssl` specifies whether to encrypt the LDAP transport for the idmap backend.

        `NSS` idmap backend options:
        `linked_service` specifies the auxiliary directory service ID provider.

        `RFC2307` idmap backend options:
        `domain` specifies the domain for which the idmap backend is being created. Numeric id, short-form
        domain name, or long-form DNS domain name of the domain may be specified. Entry must be entered as
        it appears in `idmap.domain`.

        `range_low` and `range_high` specify the UID and GID range for which this backend is authoritative.

        `ldap_server` defines the type of LDAP server to use. This can either be an LDAP server provided
        by the Active Directory Domain (ad) or a stand-alone LDAP server.

        `bind_path_user` specfies the search base where user objects can be found in the LDAP server.

        `bind_path_group` specifies the search base where group objects can be found in the LDAP server.

        `user_cn` query cn attribute instead of uid attribute for the user name in LDAP.

        `realm` append @realm to cn for groups (and users if user_cn is set) in LDAP queries.

        `ldmap_domain` when using the LDAP server in the Active Directory server, this allows one to
        specify the domain where to access the Active Directory server. This allows using trust relationships
        while keeping all RFC 2307 records in one place. This parameter is optional, the default is to access
        the AD server in the current domain to query LDAP records.

        `ldap_url` when using a stand-alone LDAP server, this parameter specifies the LDAP URL for accessing the LDAP server.

        `ldap_user_dn` defines the user DN to be used for authentication.

        `ldap_user_dn_password` is the password to be used for LDAP authentication.

        `realm` defines the realm to use in the user and group names. This is only required when using cn_realm together with
         a stand-alone ldap server.

        `RID` backend options:
        `sssd_compat` generate idmap low range based on same algorithm that SSSD uses by default.
        """
        verrors = ValidationErrors()

        if 'options' not in data:
            data['options'] = {}

        old = await self.query()
        if data['name'] in [x['name'] for x in old]:
            verrors.add('idmap_domain_create.name', 'Domain names must be unique.')

        if data['options'].get('sssd_compat'):
            if await self.middleware.call('activedirectory.get_state') != 'HEALTHY':
                verrors.add('idmap_domain_create.options',
                            'AD service must be enabled and started to '
                            'generate an SSSD-compatible id range')
                verrors.check()

            data['range_low'] = await self.get_sssd_low_range(data['name'])
            data['range_high'] = data['range_low'] + 100000000

        await self.validate('idmap_domain_create', data, verrors)
        await self.validate_options('idmap_domain_create', data, verrors)
        if data.get('certificate_id') and not data['options'].get('ssl'):
            verrors.add('idmap_domain_create.certificate_id',
                        f'The {data["idmap_backend"]} idmap backend does not '
                        'generate LDAP traffic. Certificates do not apply.')
        verrors.check()

        if data['options'].get('ldap_user_dn_password'):
            try:
                DSType[data["name"]]
                domain = (await self.middleware.call("smb.config"))['workgroup']
            except KeyError:
                domain = data["name"]

            client_conf = await self.idmap_conf_to_client_config(data)
            await self.middleware.call(
                'ldapclient.validate_credentials',
                client_conf
            )

            secret = data['options'].pop('ldap_user_dn_password')

            await self.middleware.call("directoryservices.set_ldap_secret",
                                       domain, secret)
            await self.middleware.call("directoryservices.backup_secrets")

        final_options = IdmapBackend[data['idmap_backend']].defaults()
        final_options.update(data['options'])
        data['options'] = final_options

        id_ = await self.middleware.call(
            'datastore.insert', self._config.datastore,
            data, {'prefix': self._config.datastore_prefix}
        )
        out = await self.query([('id', '=', id_)], {'get': True})
        await self.synchronize()
        return out

    async def do_update(self, id_, data):
        """
        Update a domain by id.
        """

        old = await self.query([('id', '=', id_)], {'get': True})
        new = old.copy()
        new.update(data)
        if data.get('idmap_backend') and data['idmap_backend'] != old['idmap_backend']:
            """
            Remove options from previous backend because they are almost certainly
            not valid for the new backend.
            """
            new['options'] = data.get('options', {})
        else:
            new['options'] = old['options'].copy() | data.get('options', {})

        tmp = data.copy()
        verrors = ValidationErrors()
        if old['name'] in [x.name for x in DSType] and old['name'] != new['name']:
            verrors.add('idmap_domain_update.name',
                        f'Changing name of default domain {old["name"]} is not permitted')

        if new['options'].get('sssd_compat') and not old['options'].get('sssd_compat'):
            if await self.middleware.call('activedirectory.get_state') != 'HEALTHY':
                verrors.add('idmap_domain_update.options',
                            'AD service must be enabled and started to '
                            'generate an SSSD-compatible id range')
                verrors.check()

            new['range_low'] = await self.get_sssd_low_range(new['name'])
            new['range_high'] = new['range_low'] + 100000000

        if new['idmap_backend'] == 'AUTORID' and new['name'] != 'DS_TYPE_ACTIVEDIRECTORY':
            verrors.add("idmap_domain_update.idmap_backend",
                        "AUTORID is only permitted for the default idmap backend for "
                        "the active directory directory service (DS_TYPE_ACTIVEDIRECTORY).")

        await self.validate('idmap_domain_update', new, verrors)
        await self.validate_options('idmap_domain_update', new, verrors, ['MISSING'])
        tmp['idmap_backend'] = new['idmap_backend']
        if data.get('options'):
            await self.validate_options('idmap_domain_update', tmp, verrors, ['EXTRA'])

        if data.get('certificate_id') and not data['options'].get('ssl'):
            verrors.add('idmap_domain_update.certificate_id',
                        f'The {new["idmap_backend"]} idmap backend does not '
                        'generate LDAP traffic. Certificates do not apply.')
        verrors.check()
        await self.prune_keys(new)
        final_options = IdmapBackend[new['idmap_backend']].defaults() | new['options'].copy()
        new['options'] = final_options

        if new['options'].get('ldap_user_dn_password'):
            try:
                DSType[new["name"]]
                domain = (await self.middleware.call("smb.config"))['workgroup']
            except KeyError:
                domain = new["name"]

            client_conf = await self.idmap_conf_to_client_config(new)
            await self.middleware.call(
                'ldapclient.validate_credentials',
                client_conf
            )

            secret = new['options'].pop('ldap_user_dn_password')
            await self.middleware.call("directoryservices.set_ldap_secret",
                                       domain, secret)
            await self.middleware.call("directoryservices.backup_secrets")

        await self.middleware.call(
            'datastore.update', self._config.datastore,
            new['id'], new, {'prefix': self._config.datastore_prefix}
        )

        out = await self.query([('id', '=', id_)], {'get': True})
        await self.synchronize(False)
        cache_job = await self.middleware.call('idmap.clear_idmap_cache')
        await cache_job.wait()
        return out

    async def do_delete(self, id_):
        """
        Delete a domain by id. Deletion of default system domains is not permitted.
        """
        entry = await self.get_instance(id_)
        if entry['name'] in DSType.choices():
            raise CallError(f'Deleting system idmap domain [{entry["name"]}] is not permitted.', errno.EPERM)

        ret = await self.middleware.call('datastore.delete', self._config.datastore, id_)
        await self.synchronize()
        return ret

    def _pyuidgid_to_dict(self, entry):
        if entry.id_type == IDType.USER.wbc_const():
            idtype = 'USER'
        elif entry.id_type == IDType.GROUP.wbc_const():
            idtype = 'GROUP'
        else:
            idtype = 'BOTH'
        return {
            'id_type': idtype,
            'id': entry.id,
            'sid': entry.sid
        }

    @private
    async def name_to_sid(self, name):
        try:
            client = self.__wbclient_ctx()
            entry = client.name_to_uidgid_entry(name)

        except wbclient.WBCError as e:
            raise CallError(str(e), WBCErr[e.error_code], e.error_code)

        except MatchNotFound:
            return {'sid': None, 'type': wbclient.SID_TYPE_NAME_INVALID}

        return {
            'sid': entry.sid,
            'type': entry.sid_type['raw']
        }

    @private
    def convert_sids(self, sidlist):
        """
        Internal bulk conversion method Windows-style SIDs to Unix IDs (uid or gid)
        This ends up being a de-facto wrapper around wbcCtxSidsToUnixIds from
        libwbclient (single winbindd request), and so it is the preferred
        method of batch conversion.
        """
        if not sidlist:
            raise CallError("List of SIDS to convert must contain at least one entry")

        try:
            client = self.__wbclient_ctx()
            results = client.sids_to_users_and_groups(sidlist)
        except wbclient.WBCError as e:
            raise CallError(str(e), WBCErr[e.error_code], e.error_code)

        mapped = {sid: {
            'type': IDType.parse_wbc_id_type(entry.id_type),
            'id': entry.id,
            'name': f'{entry.domain}{client.ctx.separator.decode()}{entry.name}',
        } for sid, entry in results['mapped'].items()}

        return {'mapped': mapped, 'unmapped': results['unmapped']}

    @private
    def convert_unixids(self, id_list):
        """
        Internal bulk conversion method for Unix IDs (uid or gid) to Windows-style
        SIDs. This ends up being a de-facto wrapper around wbcCtxUnixIdsToSids
        from libwbclient (single winbindd request), and so it is the preferred
        method of batch conversion.
        """
        payload = []
        for entry in id_list:
            unixid = entry.get("id")
            id_ = IDType[entry.get("id_type", "GROUP")]
            payload.append({
                'id_type': id_.wbc_str(),
                'id': unixid
            })

        try:
            client = self.__wbclient_ctx()
            results = client.users_and_groups_to_sids(payload)
        except wbclient.WBCError as e:
            raise CallError(str(e), WBCErr[e.error_code], e.error_code)

        mapped = {unixid: {
            'type': entry.sid_type['parsed'][4:],
            'id': entry.id,
            'sid': entry.sid,
            'name': f'{entry.domain}{client.ctx.separator.decode()}{entry.name}',
        } for unixid, entry in results['mapped'].items()}

        unmapped = {unixid: {
            'id_type': 'GROUP' if unixid.startswith('GID') else 'USER',
            'id': entry.id,
        } for unixid, entry in results['unmapped'].items()}

        return {'mapped': mapped, 'unmapped': unmapped}

    @private
    def sid_to_name(self, sid):
        try:
            client = self.__wbclient_ctx()
            entry = client.sid_to_uidgid_entry(sid)
        except wbclient.WBCError as e:
            raise CallError(str(e), WBCErr[e.error_code], e.error_code)

        return {
            'name': f'{entry.domain}{client.ctx.separator.decode()}{entry.name}',
            'type': entry.sid_type['raw']
        }

    @private
    def sid_to_unixid(self, sid_str):
        if sid_str.startswith(SID_LOCAL_USER_PREFIX):
            return {"id_type": "USER", "id": int(sid_str.strip(SID_LOCAL_USER_PREFIX))}

        elif sid_str.startswith(SID_LOCAL_GROUP_PREFIX):
            return {"id_type": "GROUP", "id": int(sid_str.strip(SID_LOCAL_GROUP_PREFIX))}

        try:
            client = self.__wbclient_ctx()
            entry = client.sid_to_uidgid_entry(sid_str)
        except MatchNotFound:
            return None

        return self._pyuidgid_to_dict(entry)

    @private
    @filterable
    async def builtins(self, filters, options):
        out = []
        idmap_backend = await self.middleware.call("smb.getparm", "idmap config * : backend", "GLOBAL")
        if idmap_backend != "tdb":
            """
            idmap_autorid and potentially other allocating idmap backends may be used for
            the default domain.
            """
            return []

        idmap_range = await self.middleware.call("smb.getparm", "idmap config * : range", "GLOBAL")
        low_range = int(idmap_range.split("-")[0].strip())
        for idx, entry in enumerate(WELL_KNOWN_SIDS):
            finalized_entry = entry.copy()
            finalized_entry.update({
                'id': idx,
                'gid': low_range + 3 + idx
            })
            out.append(finalized_entry)

        return filter_list(out, filters, options)

    @private
    async def id_to_name(self, id_, id_type):
        idtype = IDType[id_type]
        idmap_timeout = 5.0

        if idtype == IDType.GROUP or idtype == IDType.BOTH:
            method = "group.get_group_obj"
            to_check = {"gid": id_}
            key = 'gr_name'
        elif idtype == IDType.USER:
            method = "user.get_user_obj"
            to_check = {"uid": id_}
            key = 'pw_name'
        else:
            raise CallError(f"Unsupported id_type: [{idtype.name}]")

        try:
            ret = await asyncio.wait_for(
                self.middleware.create_task(self.middleware.call(method, to_check)),
                timeout=idmap_timeout
            )
            name = ret[key]
        except asyncio.TimeoutError:
            self.logger.debug(
                "timeout encountered while trying to convert %s id %s "
                "to name. This may indicate significant networking issue.",
                id_type.lower(), id_
            )
            name = None
        except KeyError:
            name = None

        return name

    @private
    def unixid_to_sid(self, data):
        """
        Samba generates SIDs for local accounts that lack explicit mapping in
        passdb.tdb or group_mapping.tdb with a prefix of S-1-22-1 (users) and
        S-1-22-2 (groups). This is not returned by wbinfo, but for consistency
        with what appears when viewed over SMB protocol we'll do the same here.
        """
        unixid = data.get("id")
        id_ = IDType[data.get("id_type", "GROUP")]
        payload = {
            'id_type': id_.wbc_str(),
            'id': unixid
        }
        try:
            client = self.__wbclient_ctx()
            entry = client.uidgid_to_sid(payload)
        except MatchNotFound:
            is_local = self.middleware.call_sync(
                f'{"user" if id_ == IDType.USER else "group"}.query',
                [("uid" if id_ == IDType.USER else "gid", '=', unixid)],
                {"count": True}
            )
            if is_local:
                return f'S-1-22-{1 if id_ == IDType.USER else 2}-{unixid}'

            return None
        except wbclient.WBCError as e:
            raise CallError(str(e), WBCErr[e.error_code], e.error_code)

        return self._pyuidgid_to_dict(entry)['sid']

    @private
    async def get_idmap_info(self, ds, id_):
        low_range = None
        id_type_both = False
        domains = await self.query()

        for d in domains:
            if ds == 'activedirectory' and d['name'] == 'DS_TYPE_LDAP':
                continue

            if ds == 'ldap' and d['name'] != 'DS_TYPE_LDAP':
                continue

            if id_ in range(d['range_low'], d['range_high']):
                low_range = d['range_low']
                id_type_both = d['idmap_backend'] in ['AUTORID', 'RID']
                break

        return (low_range, id_type_both)

    @private
    async def synthetic_user(self, ds, passwd):
        idmap_info = await self.get_idmap_info(ds, passwd['pw_uid'])
        if idmap_info[0] is None:
            # ID doesn't match one of our configured idmap ranges.
            # This means it's probably local
            return None

        sid = await self.middleware.run_in_thread(self.unixid_to_sid, {"id": passwd['pw_uid'], "id_type": "USER"})
        rid = int(sid.rsplit('-', 1)[1])
        return {
            'id': 100000 + idmap_info[0] + rid,
            'uid': passwd['pw_uid'],
            'username': passwd['pw_name'],
            'unixhash': None,
            'smbhash': None,
            'group': {},
            'home': '',
            'shell': '',
            'full_name': passwd['pw_gecos'],
            'builtin': False,
            'email': '',
            'password_disabled': False,
            'locked': False,
            'sudo_commands': [],
            'sudo_commands_nopasswd': [],
            'attributes': {},
            'groups': [],
            'sshpubkey': None,
            'local': False,
            'id_type_both': idmap_info[1],
        }

    @private
    async def synthetic_group(self, ds, grp):
        idmap_info = await self.get_idmap_info(ds, grp['gr_gid'])
        if idmap_info[0] is None:
            # ID doesn't match one of our configured idmap ranges.
            # This means it's probably local
            return None

        sid = await self.middleware.run_in_thread(self.unixid_to_sid, {"id": grp['gr_gid'], "id_type": "GROUP"})
        rid = int(sid.rsplit('-', 1)[1])
        return {
            'id': 100000 + idmap_info[0] + rid,
            'gid': grp['gr_gid'],
            'name': grp['gr_name'],
            'group': grp['gr_name'],
            'builtin': False,
            'sudo_commands': [],
            'sudo_commands_nopasswd': [],
            'users': [],
            'local': False,
            'id_type_both': idmap_info[1],
        }

    @private
    async def idmap_to_smbconf(self, data=None):
        rv = {}
        if data is None:
            idmap = await self.query()
        else:
            idmap = data

        ds_state = await self.middleware.call('directoryservices.get_state')
        workgroup = await self.middleware.call('smb.getparm', 'workgroup', 'global')
        ad_enabled = ds_state['activedirectory'] in ['HEALTHY', 'JOINING', 'FAULTED']
        ldap_enabled = ds_state['ldap'] in ['HEALTHY', 'JOINING', 'FAULTED']
        ad_idmap = filter_list(idmap, [('name', '=', DSType.DS_TYPE_ACTIVEDIRECTORY.name)], {'get': True}) if ad_enabled else None
        disable_ldap_starttls = False

        for i in idmap:
            if i['name'] == DSType.DS_TYPE_DEFAULT_DOMAIN.name:
                if ad_idmap and ad_idmap['idmap_backend'] == 'AUTORID':
                    continue
                domain = "*"
            elif i['name'] == DSType.DS_TYPE_ACTIVEDIRECTORY.name:
                if not ad_enabled:
                    continue
                if i['idmap_backend'] == 'AUTORID':
                    domain = "*"
                else:
                    domain = workgroup
            elif i['name'] == DSType.DS_TYPE_LDAP.name:
                if not ldap_enabled:
                    continue
                domain = workgroup
                if i['idmap_backend'] == 'LDAP':
                    """
                    In case of default LDAP backend, populate values from ldap form.
                    """
                    idmap_prefix = f"idmap config {domain} :"
                    ldap = await self.middleware.call('ldap.config')
                    rv.update({
                        f"{idmap_prefix} backend": {"raw": i['idmap_backend'].lower()},
                        f"{idmap_prefix} range": {"raw": f"{i['range_low']} - {i['range_high']}"},
                        f"{idmap_prefix} ldap_base_dn": {"raw": ldap['basedn']},
                        f"{idmap_prefix} ldap_url": {"raw": ' '.join(ldap['uri_list'])},
                    })
                    continue
            else:
                domain = i['name']

            idmap_prefix = f"idmap config {domain} :"
            rv.update({
                f"{idmap_prefix} backend": {"raw": i['idmap_backend'].lower()},
                f"{idmap_prefix} range": {"raw": f"{i['range_low']} - {i['range_high']}"}
            })
            for k, v in i['options'].items():
                backend_parameter = "realm" if k == "cn_realm" else k
                if k == 'ldap_server':
                    v = 'ad' if v == 'AD' else 'stand-alone'
                elif k == 'ldap_url':
                    v = f'{"ldaps://" if i["options"]["ssl"]  == "ON" else "ldap://"}{v}'
                elif k == 'ssl':
                    if v != 'STARTTLS':
                        disable_ldap_starttls = True

                    continue

                rv.update({
                    f"{idmap_prefix} {backend_parameter}": {"parsed": v},
                })

        if ad_enabled:
            rv['ldap ssl'] = {'parsed': 'off' if disable_ldap_starttls else 'start tls'}

        return rv

    @private
    async def diff_conf_and_registry(self, data, idmaps):
        r = idmaps
        s_keys = set(data.keys())
        r_keys = set(r.keys())
        intersect = s_keys.intersection(r_keys)
        return {
            'added': {x: data[x] for x in s_keys - r_keys},
            'removed': {x: r[x] for x in r_keys - s_keys},
            'modified': {x: data[x] for x in intersect if data[x] != r[x]},
        }

    @private
    async def synchronize(self, restart=True):
        config_idmap = await self.query()
        idmaps = await self.idmap_to_smbconf(config_idmap)
        to_check = (await self.middleware.call('smb.reg_globals'))['idmap']
        diff = await self.diff_conf_and_registry(idmaps, to_check)
        await self.middleware.call('sharing.smb.apply_conf_diff', 'GLOBAL', diff)
        if restart:
            await self.middleware.call('service.restart', 'idmap')
