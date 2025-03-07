from middlewared.service import Service, job, private
from middlewared.service_exception import CallError
from middlewared.utils import run
from middlewared.plugins.smb import SMBCmd, SMBBuiltin, SMBPath

import os
import json
import tdb
import struct

# This follows JSON output version for net_groupmap.c
# Output format may change between this and final version accepted
# upstream, but Samba project has standardized on following version format
GROUPMAP_JSON_VERSION = {"major": 0, "minor": 1}
WINBINDD_AUTO_ALLOCATED = ['S-1-5-32-544', 'S-1-5-32-545', 'S-1-5-32-546']
WINBINDD_WELL_KNOWN_PADDING = 100


class SMBService(Service):

    class Config:
        service = 'cifs'
        service_verb = 'restart'

    @private
    async def json_check_version(self, version):
        if version == GROUPMAP_JSON_VERSION:
            return

        raise CallError(
            "Unexpected JSON version returned from Samba utils: "
            f"[{version}]. Expected version was: [{GROUPMAP_JSON_VERSION}]. "
            "Behavior is undefined with a version mismatch and so refusing "
            "to perform groupmap operation. Please file a bug report at "
            "jira.ixsystems.com with this traceback."
        )

    @private
    async def groupmap_listmem(self, sid):
        payload = json.dumps({"alias": sid})
        lm = await run([
            SMBCmd.NET.value, "--json", "groupmap", "listmem", payload
        ], check=False)

        # Command will return ENOENT when fails with STATUS_NO_SUCH_ALIAS
        if lm.returncode == 2:
            return []
        elif lm.returncode != 0:
            raise CallError(f"Failed to list membership of alias [{sid}]: "
                            f"{lm.stderr.decode()}")

        output = json.loads(lm.stdout.decode())
        await self.json_check_version(output['version'])

        return [x["sid"] for x in output['members']]

    @private
    async def groupmap_addmem(self, alias, member):
        payload = f'data={json.dumps({"alias": alias, "member": member})}'
        am = await run([
            SMBCmd.NET.value, "--json", "groupmap", "addmem", payload,
        ], check=False)
        if am.returncode != 0:
            raise CallError(
                f"Failed to add [{member}] to [{alias}]: {am.stderr.decode()}"
            )

    @private
    async def diff_membership(self, actual, expected):
        """
        Generate a diff between expected members of an alias vs
        actual members. This is used for batch operation to add
        or remove memberships. Since these memberships affect
        how nss_winbind generates passwd entries, and also rights
        evaluation in samba (for instance when a non-owner tries
        to change ownership of a file), it is important that
        we have no unexpected entries here.
        """
        out = {"ADDMEM": [], "DELMEM": []}

        actual_set = set(actual)
        expected_set = set(expected)

        out["ADDMEM"] = [{"sid": x} for x in expected_set - actual_set]
        out["DELMEM"] = [{"sid": x} for x in actual_set - expected_set]

        return out

    @private
    async def update_payload_with_diff(self, payload, alias, diff, ad):
        async def add_to_payload(payload, alias, key, members):
            idx = next((i for i, x in enumerate(payload[key]) if x["alias"] == alias), None)
            if not idx:
                payload[key].append({
                    "alias": alias,
                    "members": members,
                })
            else:
                payload[key][idx]["members"].append(members)

        if diff.get("ADDMEM"):
            await add_to_payload(payload, alias, "ADDMEM", diff["ADDMEM"])

        """
        If AD is FAULTED or in process of joining or leaving AD,
        then we may not have an accurate picture of what should be
        in the alias member list. In this case, defer member removal
        until next groupmap synchronization.
        """
        if ad in ["HEALTHY", "DISABLED"] and diff.get("DELMEM"):
            await add_to_payload(payload, alias, "DELMEM", diff["DELMEM"])

        return

    @private
    async def sync_foreign_groups(self):
        """
        Domain Users, and Domain Admins must have S-1-5-32-545 and S-1-5-32-544
        added to their respective Unix tokens for correct behavior in AD domain.
        These are added by making them foreign members in the group_mapping for
        the repsective alias. This membership is generated during samba startup
        when newly creating these groups (if they don't exist), but can get
        lost, resulting in unexpected / erratic permissions behavior.
        """
        domain_sid = None
        payload = {"ADDMEM": [], "DELMEM": []}
        # second groupmap listing is to ensure we have accurate / current info.
        groupmap = await self.groupmap_list()
        admin_group = (await self.middleware.call('smb.config'))['admin_group']

        ad_state = await self.middleware.call('activedirectory.get_state')
        if ad_state == 'HEALTHY':
            try:
                domain_info = await self.middleware.call('idmap.domain_info',
                                                         'DS_TYPE_ACTIVEDIRECTORY')
                domain_sid = domain_info['sid']
            except Exception:
                self.logger.warning('Failed to retrieve idmap domain info', exc_info=True)

        """
        Administrators should only have local and domain admins, and a user-
        designated "admin group" (if specified).
        """
        admins = await self.groupmap_listmem("S-1-5-32-544")
        expected = [groupmap['local_builtins'][544]['sid']]
        if domain_sid:
            expected.append(f'{domain_sid}-512')

        if admin_group:
            admin_sid = None
            grp_obj = await self.middleware.call(
                'group.query',
                [('group', '=', admin_group)],
                {'extra': {'additional_information': ['SMB', 'DS']}}
            )
            if grp_obj:
                admin_sid = grp_obj[0]['sid']

            if admin_sid:
                expected.append(admin_sid)

        diff = await self.diff_membership(admins, expected)
        await self.update_payload_with_diff(payload, "S-1-5-32-544", diff, ad_state)

        # Users should only have local users and domain users
        users = await self.groupmap_listmem("S-1-5-32-545")
        if domain_sid:
            expected.append(f'{domain_sid}-513')

        diff = await self.diff_membership(users, expected)
        await self.update_payload_with_diff(payload, "S-1-5-32-545", diff, ad_state)

        guests = await self.groupmap_listmem("S-1-5-32-546")
        expected = [
            groupmap['local_builtins'][546]['sid'],
            f'{groupmap["localsid"]}-501'
        ]
        if domain_sid:
            expected.append(f'{domain_sid}-514')

        diff = await self.diff_membership(guests, expected)
        await self.update_payload_with_diff(payload, "S-1-5-32-546", diff, ad_state)

        await self.batch_groupmap(payload)

    @private
    def initialize_idmap_tdb(self, low_range):
        tdb_path = f'{SMBPath.STATEDIR.platform()}/winbindd_idmap.tdb'
        tdb_flags = tdb.DEFAULT
        open_flags = os.O_CREAT | os.O_RDWR

        try:
            tdb_handle = tdb.Tdb(tdb_path, 0, tdb_flags, open_flags, 0o644)
        except Exception:
            self.logger.warning("Failed to create winbindd_idmap.tdb", exc_info=True)
            return None

        try:
            for key, val in [
                (b'IDMAP_VERSION\x00', 2),
                (b'USER HWM\x00', low_range),
                (b'GROUP HWM\x00', low_range)
            ]:
                tdb_handle.store(key, struct.pack("<L", val))
        except Exception:
            self.logger.warning('Failed to initialize winbindd_idmap.tdb', exc_info=True)
            tdb_handle.close()
            return None

        return tdb_handle

    @private
    def validate_groupmap_hwm(self, low_range):
        """
        Middleware forces allocation of GIDs for Users, Groups, and Administrators
        to be deterministic with the default idmap backend. Bump up the idmap_tdb
        high-water mark to avoid conflicts with these and remove any mappings that
        conflict. Winbindd will regenerate the removed ones as-needed.
        """
        def add_key(tdb_handle, gid, sid):
            gid_val = f'GID {gid}\x00'.encode()
            sid_val = f'{sid}\x00'.encode()
            tdb_handle.store(gid_val, sid_val)
            tdb_handle.store(sid_val, gid_val)

        def remove_key(tdb_handle, key, reverse):
            tdb_handle.delete(key)
            if reverse:
                tdb_handle.delete(reverse)

        must_reload = False
        len_wb_groups = len(WINBINDD_AUTO_ALLOCATED)
        builtins = self.middleware.call_sync('idmap.builtins')

        try:
            tdb_handle = tdb.open(f"{SMBPath.STATEDIR.platform()}/winbindd_idmap.tdb")
        except FileNotFoundError:
            tdb_handle = self.initialize_idmap_tdb(low_range)
            if not tdb_handle:
                return False

        try:
            tdb_handle.transaction_start()
            group_hwm_bytes = tdb_handle.get(b'GROUP HWM\00')
            hwm = struct.unpack("<L", group_hwm_bytes)[0]
            if hwm < low_range + len_wb_groups + len(builtins):
                hwm = low_range + len_wb_groups + len(builtins) + WINBINDD_WELL_KNOWN_PADDING
                new_hwm_bytes = struct.pack("<L", hwm)
                tdb_handle.store(b'GROUP HWM\00', new_hwm_bytes)
                must_reload = True

            for key in tdb_handle.keys():
                # sample key: b'GID 9000020\x00'
                if key[:3] == b'GID' and int(key.decode()[4:-1]) < (low_range + len_wb_groups):
                    reverse = tdb_handle.get(key)
                    remove_key(tdb_handle, key, reverse)
                    must_reload = True

            for entry in builtins:
                if not entry['set']:
                    continue

                sid_key = f'{entry["sid"]}\x00'.encode()
                val = tdb_handle.get(f'{entry["sid"]}\x00'.encode())
                if val is None or val.decode() != f'GID {entry["gid"]}\x00':
                    if sid_key in tdb_handle.keys():
                        self.logger.debug(
                            "incorrect sid mapping detected %s -> %s"
                            "replacing with %s -> %s",
                            entry['sid'], val.decode()[4:-1] if val else "None",
                            entry['sid'], entry['gid']
                        )
                        remove_key(tdb_handle, f'{entry["sid"]}\x00'.encode(), val)

                    add_key(tdb_handle, entry['gid'], entry['sid'])
                    must_reload = True

            tdb_handle.transaction_commit()

        except Exception as e:
            tdb_handle.transaction_cancel()
            self.logger.warning("TDB maintenace failed: %s", e)

        finally:
            tdb_handle.close()

        if must_reload:
            self.middleware.call_sync('idmap.snapshot_samba4_dataset')

        return must_reload

    @private
    async def groupmap_list(self):
        """
        Convert JSON groupmap output to dict to get O(1) lookups by `gid`

        Separate out the groupmap output into builtins, locals, and invalid entries.
        Invalid entries are ones that aren't from our domain, or are mapped to gid -1.
        Latter occurs when group mapping is lost. In case of invalid entries, we store
        list of SIDS to be removed. SID is necessary and sufficient for groupmap removal.
        """
        rv = {"builtins": {}, "local": {}, "local_builtins": {}, "invalid": []}
        passdb_backend = await self.middleware.call('smb.getparm', 'passdb backend', 'global')
        if not passdb_backend.startswith('tdbsam'):
            return rv

        localsid = await self.middleware.call('smb.get_system_sid')
        if localsid is None:
            raise CallError("Unable to retrieve local system SID. Group mapping failure.")

        out = await run([SMBCmd.NET.value, '--json', 'groupmap', 'list', '{"verbose": true}'], check=False)
        if out.returncode != 0:
            raise CallError(f'groupmap list failed with error {out.stderr.decode()}')

        gm = json.loads(out.stdout.decode())
        await self.json_check_version(gm['version'])

        for g in gm['groupmap']:
            gid = g['gid']
            key = 'invalid'
            if gid == -1:
                rv[key].append(g['sid'])
                continue

            if g['sid'].startswith("S-1-5-32"):
                key = 'builtins'
            elif g['sid'].startswith(localsid) and g['gid'] in (544, 546):
                key = 'local_builtins'
            elif g['sid'].startswith(localsid):
                key = 'local'

            if key == 'invalid' or rv[key].get(gid):
                rv['invalid'].append(g['sid'])
                continue

            rv[key][gid] = g

        rv["localsid"] = localsid
        return rv

    @private
    async def sync_builtins(self, groupmap):
        idmap_backend = await self.middleware.call("smb.getparm", "idmap config * : backend", "GLOBAL")
        idmap_range = await self.middleware.call("smb.getparm", "idmap config * : range", "GLOBAL")
        payload = {"ADD": [{"groupmap": []}], "MOD": [{"groupmap": []}], "DEL": [{"groupmap": []}]}
        must_reload = False

        if idmap_backend != "tdb":
            """
            idmap_autorid and potentially other allocating idmap backends may be used for
            the default domain. We do not want to touch how these are allocated.
            """
            return must_reload

        low_range = int(idmap_range.split("-")[0].strip())
        sid_lookup = {x["sid"]: x for x in groupmap.values()}

        for b in (SMBBuiltin.ADMINISTRATORS, SMBBuiltin.USERS, SMBBuiltin.GUESTS):
            sid = b.value[1]
            rid = int(sid.split('-')[-1])
            gid = low_range + (rid - 544)
            entry = sid_lookup.get(sid, None)
            if entry and entry['gid'] == gid:
                # Value is correct, nothing to do.
                continue

            # If group type is incorrect, it entry must be deleted before re-adding.
            elif entry and entry['gid'] != gid and entry['group_type_int'] != 4:
                payload['DEL'][0]['groupmap'].append({
                    'sid': str(sid),
                })
                payload['ADD'][0]['groupmap'].append({
                    'sid': str(sid),
                    'gid': gid,
                    'group_type_str': 'local',
                    'nt_name': b.value[0][8:].capitalize()
                })
            elif entry and entry['gid'] != gid:
                payload['MOD'][0]['groupmap'].append({
                    'sid': str(sid),
                    'gid': gid,
                    'group_type_str': 'local',
                    'nt_name': b.value[0][8:].capitalize()
                })
            else:
                payload['ADD'][0]['groupmap'].append({
                    'sid': str(sid),
                    'gid': gid,
                    'group_type_str': 'local',
                    'nt_name': b.value[0][8:].capitalize()
                })

        await self.batch_groupmap(payload)
        if (await self.middleware.call('smb.validate_groupmap_hwm', low_range)):
            must_reload = True

        return must_reload

    @private
    async def batch_groupmap(self, data):
        for op in ["ADD", "MOD", "DEL"]:
            if data.get(op) is not None and len(data[op]) == 0:
                data.pop(op)

        payload = json.dumps(data)
        out = await run([SMBCmd.NET.value, '--json', 'groupmap', 'batch', payload], check=False)
        if out.returncode != 0:
            raise CallError(f'Batch operation for [{data}] failed with error {out.stderr.decode()}')

    @private
    @job(lock="groupmap_sync")
    async def synchronize_group_mappings(self, job, bypass_sentinel_check=False):
        """
        This method does the following:
        1) prepares payload for a batch groupmap operation. These are added to two arrays:
           "to_add" and "to_del". Missing entries are added, invalid entries are deleted.
        2) we synchronize S-1-5-32-544, S-1-5-32-545, and S-1-5-32-546 separately
        3) we add any required group mappings for the SIDs in (2) above.
        4) we flush various caches if required.
        """
        payload = {}
        to_add = []
        to_del = []

        if await self.middleware.call('ldap.get_state') != "DISABLED":
            return

        if not bypass_sentinel_check and not await self.middleware.call('smb.is_configured'):
            raise CallError(
                "SMB server configuration is not complete. "
                "This may indicate system dataset setup failure."
            )

        groupmap = await self.groupmap_list()
        must_remove_cache = False

        groups = await self.middleware.call('group.query', [('builtin', '=', False), ('smb', '=', True)])
        g_dict = {x["gid"]: x for x in groups}
        g_dict[545] = await self.middleware.call('group.query', [('gid', '=', 545)], {'get': True})

        intersect = set(g_dict.keys()).intersection(set(groupmap["local"].keys()))

        set_to_add = set(g_dict.keys()) - set(groupmap["local"].keys())
        set_to_del = set(groupmap["local"].keys()) - set(g_dict.keys())
        set_to_mod = set([x for x in intersect if groupmap['local'][x]['nt_name'] != g_dict[x]['group']])

        to_add = [{
            "dbid": g_dict[x]["id"],
            "gid": g_dict[x]["gid"],
            "nt_name": g_dict[x]["group"],
            "group_type_str": "local"
        } for x in set_to_add]

        to_mod = [{
            "gid": g_dict[x]["gid"],
            "nt_name": g_dict[x]["group"],
            "sid": groupmap["local"][x]["sid"],
            "group_type_str": "local"
        } for x in set_to_mod]

        to_del = [{
            "sid": groupmap["local"][x]["sid"]
        } for x in set_to_del]

        for sid in groupmap['invalid']:
            to_del.append({"sid": sid})

        for gid in (544, 546):
            if not groupmap["local_builtins"].get(gid):
                builtin = SMBBuiltin.by_rid(gid)
                rid = 512 + (gid - 544)
                sid = f'{groupmap["localsid"]}-{rid}'
                to_add.append({
                    "gid": gid,
                    "nt_name": f"local_{builtin.name.lower()}",
                    "group_type_str": "local",
                    "sid": sid,
                })

        if to_add:
            payload["ADD"] = [{"groupmap": to_add}]

        if to_mod:
            payload["MOD"] = [{"groupmap": to_mod}]

        if to_del:
            payload["DEL"] = [{"groupmap": to_del}]

        await self.middleware.call('smb.fixsid')
        must_remove_cache = await self.sync_builtins(groupmap['builtins'])
        await self.batch_groupmap(payload)
        await self.sync_foreign_groups()

        if must_remove_cache:
            await self.middleware.call('tdb.wipe', {
                'name': f'{SMBPath.CACHE_DIR.platform()}/winbindd_cache.tdb',
                'tdb-options': {'data_type': 'STRING', 'backend': 'CUSTOM'}
            })
            flush = await run([SMBCmd.NET.value, 'cache', 'flush'], check=False)
            if flush.returncode != 0:
                self.logger.debug('Attempt to flush cache failed: %s', flush.stderr.decode().strip())
