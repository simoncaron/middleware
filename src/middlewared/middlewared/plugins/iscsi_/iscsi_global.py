import asyncio
import re
import socket

import middlewared.sqlalchemy as sa
from middlewared.async_validators import validate_port
from middlewared.schema import Bool, Dict, Int, List, Str, accepts
from middlewared.service import SystemServiceService, ValidationErrors, private
from middlewared.utils import run
from middlewared.validators import IpAddress, Port, Range

RE_IP_PORT = re.compile(r'^(.+?)(:[0-9]+)?$')


class ISCSIGlobalModel(sa.Model):
    __tablename__ = 'services_iscsitargetglobalconfiguration'

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_basename = sa.Column(sa.String(120))
    iscsi_isns_servers = sa.Column(sa.Text())
    iscsi_pool_avail_threshold = sa.Column(sa.Integer(), nullable=True)
    iscsi_alua = sa.Column(sa.Boolean(), default=False)
    iscsi_listen_port = sa.Column(sa.Integer(), nullable=False, default=3260)


class ISCSIGlobalService(SystemServiceService):

    class Config:
        datastore = 'services.iscsitargetglobalconfiguration'
        datastore_extend = 'iscsi.global.config_extend'
        datastore_prefix = 'iscsi_'
        service = 'iscsitarget'
        namespace = 'iscsi.global'
        cli_namespace = 'sharing.iscsi.global'

    @private
    def port_is_listening(self, host, port, timeout=5):
        ret = False

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout:
            s.settimeout(timeout)

        try:
            s.connect((host, port))
            ret = True
        except Exception:
            self.logger.debug("connection to %s failed", host, exc_info=True)
            ret = False
        finally:
            s.close()

        return ret

    @private
    def validate_isns_server(self, server, verrors):
        """
        Check whether a valid IP[:port] was supplied.  Returns None or failure,
        or (server, ip, port) tuple on success.
        """
        invalid_ip_port_tuple = f'Server "{server}" is not a valid IP(:PORT)? tuple.'

        reg = RE_IP_PORT.search(server)
        if not reg:
            verrors.add('iscsiglobal_update.isns_servers', invalid_ip_port_tuple)
            return None

        ip = reg.group(1)
        if ip and ip[0] == '[' and ip[-1] == ']':
            ip = ip[1:-1]

        # First check that a valid IP was supplied
        try:
            ip_validator = IpAddress()
            ip_validator(ip)
        except ValueError:
            verrors.add('iscsiglobal_update.isns_servers', invalid_ip_port_tuple)
            return None

        # Next check the port number (if supplied)
        parts = server.split(':')
        if len(parts) == 2:
            try:
                port = int(parts[1])
                port_validator = Port()
                port_validator(port)
            except ValueError:
                verrors.add('iscsiglobal_update.isns_servers', invalid_ip_port_tuple)
                return None
        else:
            port = 3205

        return (server, ip, port)

    @private
    def config_extend(self, data):
        data['isns_servers'] = data['isns_servers'].split()
        return data

    @accepts(Dict(
        'iscsiglobal_update',
        Str('basename'),
        List('isns_servers', items=[Str('server')]),
        Int('listen_port', validators=[Range(min_=1025, max_=65535)], default=3260),
        Int('pool_avail_threshold', validators=[Range(min_=1, max_=99)], null=True),
        Bool('alua'),
        update=True
    ))
    async def do_update(self, data):
        """
        `alua` is a no-op for FreeNAS.
        """
        old = await self.config()

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()

        servers = data.get('isns_servers') or []
        server_addresses = []
        for server in servers:
            if result := self.validate_isns_server(server, verrors):
                server_addresses.append(result)
        if server_addresses:
            # For the valid addresses, we will check connectivity in parallel
            coroutines = [
                self.middleware.call(
                    'iscsi.global.port_is_listening', ip, port
                ) for (server, ip, port) in server_addresses
            ]
            results = await asyncio.gather(*coroutines)
            for (server, ip, port), result in zip(server_addresses, results):
                if not result:
                    verrors.add('iscsiglobal_update.isns_servers', f'Server "{server}" could not be contacted.')

        verrors.extend(await validate_port(
            self.middleware, 'iscsiglobal_update.listen_port', new['listen_port'], 'iscsi.global'
        ))

        verrors.check()

        new['isns_servers'] = '\n'.join(servers)

        licensed = await self.middleware.call('failover.licensed')
        if licensed and old['alua'] != new['alua']:
            if not new['alua']:
                await self.middleware.call('failover.call_remote', 'service.stop', ['iscsitarget'])
                await self.middleware.call('failover.call_remote', 'iscsi.target.logout_ha_targets')

        await self._update_service(old, new, options={'ha_propagate': False})

        if licensed and old['alua'] != new['alua']:
            if new['alua']:
                await self.middleware.call('failover.call_remote', 'service.start', ['iscsitarget'])
            # Force a scst.conf update
            # When turning off ALUA we want to clean up scst.conf, and when turning it on
            # we want to give any existing target a kick to come up as a dev_disk
            await self.middleware.call('failover.call_remote', 'service.reload', ['iscsitarget'])

        # If we have just turned off iSNS then work around a short-coming in scstadmin reload
        if old['isns_servers'] != new['isns_servers'] and not servers:
            await self.middleware.call('iscsi.global.stop_active_isns')
            if licensed:
                try:
                    await self.middleware.call('failover.call_remote', 'iscsi.global.stop_active_isns')
                except Exception:
                    self.logger.error('Unhandled exception in stop_active_isns on remote controller', exc_info=True)

        return await self.config()

    @private
    async def stop_active_isns(self):
        """
        Unfortunately a SCST reload does not stop a previously active iSNS config, so
        need to be able to perform an explicit action.
        """
        cp = await run([
            'scstadmin', '-force', '-noprompt', '-set_drv_attr', 'iscsi',
            '-attributes', 'iSNSServer=""'
        ], check=False)
        if cp.returncode:
            self.logger.warning('Failed to stop active iSNS: %s', cp.stderr.decode())

    @accepts()
    async def alua_enabled(self):
        """
        Returns whether iSCSI ALUA is enabled or not.
        """
        if not await self.middleware.call('system.is_enterprise'):
            return False
        if not await self.middleware.call('failover.licensed'):
            return False

        license_ = await self.middleware.call('system.license')
        if license_ is not None and 'FIBRECHANNEL' in license_['features']:
            return True

        return (await self.middleware.call('iscsi.global.config'))['alua']
