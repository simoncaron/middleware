from datetime import datetime, timedelta
import errno
import json
import os

from middlewared.schema import accepts, Bool, Dict, Patch, returns, Str
from middlewared.service import cli_private, job, private, Service
from middlewared.utils.functools import cache
import middlewared.sqlalchemy as sa

EULA_FILE = '/usr/local/share/truenas/eula.html'
EULA_PENDING_PATH = "/data/truenas-eula-pending"

user_attrs = [
    Str('first_name'),
    Str('last_name'),
    Str('title'),
    Str('office_phone'),
    Str('mobile_phone'),
    Str('primary_email'),
    Str('secondary_email'),
    Str('address'),
    Str('city'),
    Str('state'),
    Str('zip'),
    Str('country'),
]

# We tag SMBIOS with relevant strings for each platform
# before we ship to customer. These are the various prefixes
# that represent each hardware platform.
# ('TRUENAS-X10', 'TRUENAS-M50', 'TRUENAS-MINI-X+', 'FREENAS-MINI-X', etc)
PLATFORM_PREFIXES = (
    'TRUENAS-Z',  # z-series
    'TRUENAS-X',  # x-series
    'TRUENAS-M',  # m-series AND current mini platforms
    'TRUENAS-F',  # f-series (F60, F100, F130)
    'TRUENAS-H',  # h-series (H10, H20)
    'TRUENAS-R',  # freenas certified replacement
    'FREENAS-MINI',  # minis tagged with legacy information
)


def get_chassis_hardware(dmi):
    if dmi['system-product-name'].startswith(PLATFORM_PREFIXES):
        return dmi['system-product-name']

    if dmi['baseboard-product-name'] == 'iXsystems TrueNAS X10':
        # could be that production didn't burn in the correct x-series
        # model information so let's check the motherboard model as a
        # last resort
        return 'TRUENAS-X'

    return 'TRUENAS-UNKNOWN'


class TruenasCustomerInformationModel(sa.Model):
    __tablename__ = 'truenas_customerinformation'

    id = sa.Column(sa.Integer(), primary_key=True)
    data = sa.Column(sa.Text())
    updated_at = sa.Column(sa.DateTime())
    sent_at = sa.Column(sa.DateTime(), nullable=True)
    form_dismissed = sa.Column(sa.Boolean())


class TrueNASService(Service):

    class Config:
        cli_namespace = "system.truenas"

    @accepts()
    @returns(Str('system_chassis_hardware'))
    @cli_private
    @cache
    async def get_chassis_hardware(self):
        """
        Returns what type of hardware this is, detected from dmidecode.
        """
        dmi = await self.middleware.call('system.dmidecode_info')
        return get_chassis_hardware(dmi)

    @accepts(roles=['READONLY'])
    @returns(Str('eula', max_length=None, null=True))
    @cli_private
    def get_eula(self):
        """
        Returns the TrueNAS End-User License Agreement (EULA).
        """
        if not os.path.exists(EULA_FILE):
            return
        with open(EULA_FILE, 'r', encoding='utf8') as f:
            return f.read()

    @accepts(roles=['READONLY'])
    @returns(Bool('system_eula_accepted'))
    @cli_private
    async def is_eula_accepted(self):
        """
        Returns whether the EULA is accepted or not.
        """
        return not os.path.exists(EULA_PENDING_PATH)

    @accepts()
    @returns()
    async def accept_eula(self):
        """
        Accept TrueNAS EULA.
        """
        try:
            os.unlink(EULA_PENDING_PATH)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

    @private
    async def unaccept_eula(self):
        with open(EULA_PENDING_PATH, "w"):
            pass

    # TODO: Document this please
    @accepts()
    async def get_customer_information(self):
        """
        Returns stored customer information.
        """
        result = await self.__fetch_customer_information()
        return result

    @accepts(Dict(
        'customer_information_update',
        Str('company'),
        Dict('administrative_user', *user_attrs),
        Dict('technical_user', *user_attrs),
        Dict(
            'reseller',
            Str('company'),
            Str('first_name'),
            Str('last_name'),
            Str('title'),
            Str('office_phone'),
            Str('mobile_phone'),
        ),
        Dict(
            'physical_location',
            Str('address'),
            Str('city'),
            Str('state'),
            Str('zip'),
            Str('country'),
            Str('contact_name'),
            Str('contact_phone_number'),
            Str('contact_email'),
        ),
        Str('primary_use_case'),
        Str('other_primary_use_case'),
    ))
    async def update_customer_information(self, data):
        """
        Updates customer information.
        """
        customer_information = await self.__fetch_customer_information()

        await self.middleware.call('datastore.update', 'truenas.customerinformation', customer_information["id"], {
            "data": json.dumps(data),
            "updated_at": datetime.utcnow(),
        })

        return customer_information

    async def __fetch_customer_information(self):
        result = await self.middleware.call('datastore.config', 'truenas.customerinformation')
        result["immutable_data"] = await self.__fetch_customer_information_immutable_data()
        result["data"] = json.loads(result["data"])
        result["needs_update"] = datetime.utcnow() - result["updated_at"] > timedelta(days=365)
        return result

    async def __fetch_customer_information_immutable_data(self):
        license_ = await self.middleware.call('system.license')
        if license_ is None:
            return None

        return {
            "serial_number": license_['system_serial'],
            "serial_number_ha": license_['system_serial_ha'],
            "support_level": license_['contract_type'].title(),
            "support_start_date": license_['contract_start'].isoformat(),
            "support_end_date": license_['contract_end'].isoformat(),
        }

    @accepts(roles=['READONLY'])
    @returns(Bool('is_production_system'))
    async def is_production(self):
        """
        Returns if system is marked as production.
        """
        return await self.middleware.call('keyvalue.get', 'truenas:production', False)

    @accepts(Bool('production'), Bool('attach_debug', default=False))
    @returns(Patch(
        'new_ticket_response', 'set_production',
        ('attr', {'null': True}),
    ))
    @job()
    async def set_production(self, job, production, attach_debug):
        """
        Sets system production state and optionally sends initial debug.
        """
        was_production = await self.is_production()
        await self.middleware.call('keyvalue.set', 'truenas:production', production)

        if not was_production and production:
            serial = (await self.middleware.call('system.dmidecode_info'))["system-serial-number"]
            return await job.wrap(await self.middleware.call('support.new_ticket', {
                "title": f"System has been just put into production ({serial})",
                "body": "This system has been just put into production",
                "attach_debug": attach_debug,
                "category": "Installation/Setup",
                "criticality": "Inquiry",
                "environment": "Production",
                "name": "Automatic Alert",
                "email": "auto-support@ixsystems.com",
                "phone": "-",
            }))
