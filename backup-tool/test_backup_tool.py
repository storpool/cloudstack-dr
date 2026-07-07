#!/usr/bin/env python3
"""
Unit tests for backup-tool.py.

No StorPool or CloudStack access is needed: every API call is mocked. The
real third-party modules (storpool, cs, confget) are used when installed
(e.g. in the .venv from the README) and stubbed out otherwise, so the tests
also run with a plain Python 3:

    .venv/bin/python backup-tool/test_backup_tool.py
"""
import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


class _StubApiError(Exception):
    """Stands in for storpool.spapi.ApiError (only .name is used)."""

    name = "ApiError"


def _install_missing_stubs():
    """Stub out any third-party module that is not installed."""
    try:
        import cs  # noqa: F401
    except ImportError:
        cs_stub = types.ModuleType("cs")
        cs_stub.CloudStack = object
        cs_stub.read_config = lambda: {}
        sys.modules["cs"] = cs_stub

    try:
        from storpool import spapi  # noqa: F401
    except ImportError:
        spapi_stub = types.ModuleType("storpool.spapi")
        spapi_stub.ApiError = _StubApiError
        spapi_stub.Api = object
        storpool_stub = types.ModuleType("storpool")
        storpool_stub.spapi = spapi_stub
        sys.modules["storpool"] = storpool_stub
        sys.modules["storpool.spapi"] = spapi_stub

    try:
        import confget  # noqa: F401
    except ImportError:
        confget_stub = types.ModuleType("confget")
        confget_stub.Config = object
        confget_stub.read_ini_file = lambda *a, **kw: {}
        sys.modules["confget"] = confget_stub


def _load_backup_tool():
    _install_missing_stubs()
    tool_path = Path(__file__).resolve().parent / "backup-tool.py"
    spec = importlib.util.spec_from_file_location("backup_tool", tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bt = _load_backup_tool()
ApiError = bt.spapi.ApiError


def make_api_error(name):
    """Build an ApiError carrying just ``.name``, real class or stub alike
    (the real constructor wants an HTTP status and a JSON body)."""
    err = ApiError.__new__(ApiError)
    Exception.__init__(err, name)
    err.name = name
    err.desc = name  # the real __str__ formats name and desc
    return err

SC_CONFIG_ALL = {
    "": {
        "SP_BACKUP_CLUSTER_ID": "back.b",
        "SP_BACKUP_LOCATION_NAME": "backup-loc",
        "SP_LOCAL_TEMPLATE": "tpl",
    },
}

MC_CONFIG_ALL = {
    "": {
        "SP_MULTICLUSTER": "1",
        "SP_BACKUP_CLUSTER_ID": "back.b",
        "SP_BACKUP_LOCATION_NAME": "backup-loc",
        "SP_LOCAL_TEMPLATE": "tpl",
    },
    "cluster fir.b": {
        "SP_BACKUP_CLUSTER_ID": "otherback.b",
        "SP_BACKUP_LOCATION_NAME": "other-loc",
    },
    "cluster fir.n": {},  # inherits everything from the globals
}


class BackupToolTestCase(unittest.TestCase):
    """Reset the module's globals around every test."""

    config_all = SC_CONFIG_ALL

    def setUp(self):
        self.set_config(self.config_all)
        bt.cs_api = None
        bt.sp_api = None
        bt.sp_api_mc = None

    @staticmethod
    def set_config(config_all):
        bt.config_all = copy.deepcopy(config_all)
        bt.config = bt.config_all[""]


class TestIsMulticluster(BackupToolTestCase):
    def test_truthy_values(self):
        for value in ("1", "true", "True", "YES"):
            bt.config["SP_MULTICLUSTER"] = value
            self.assertTrue(bt.is_multicluster(), value)

    def test_falsy_values(self):
        for value in ("0", "false", "no", ""):
            bt.config["SP_MULTICLUSTER"] = value
            self.assertFalse(bt.is_multicluster(), value)

    def test_unset(self):
        self.assertFalse(bt.is_multicluster())


class TestGetClusterConfig(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def test_section_overrides_globals(self):
        merged = bt.get_cluster_config("fir.b")
        self.assertEqual(merged["SP_BACKUP_CLUSTER_ID"], "otherback.b")
        self.assertEqual(merged["SP_BACKUP_LOCATION_NAME"], "other-loc")
        self.assertEqual(merged["SP_LOCAL_TEMPLATE"], "tpl")  # inherited

    def test_empty_section_inherits_globals(self):
        self.assertEqual(bt.get_cluster_config("fir.n"), bt.config)

    def test_unknown_or_none_cluster_returns_globals(self):
        self.assertEqual(bt.get_cluster_config("nope.x"), bt.config)
        self.assertEqual(bt.get_cluster_config(None), bt.config)

    def test_returns_a_copy(self):
        bt.get_cluster_config("fir.b")["SP_LOCAL_TEMPLATE"] = "changed"
        self.assertEqual(bt.config["SP_LOCAL_TEMPLATE"], "tpl")


class TestSpClusterKwargs(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def test_adds_tilde_prefix(self):
        self.assertEqual(
            bt.sp_cluster_kwargs("fir.b"), {"clusterName": "~fir.b"}
        )

    def test_keeps_existing_tilde(self):
        self.assertEqual(
            bt.sp_cluster_kwargs("~fir.b"), {"clusterName": "~fir.b"}
        )

    def test_no_cluster_name(self):
        self.assertEqual(bt.sp_cluster_kwargs(None), {})

    def test_single_cluster_mode(self):
        self.set_config(SC_CONFIG_ALL)
        self.assertEqual(bt.sp_cluster_kwargs("fir.b"), {})


class TestSpVolumeNameFromCsVolume(BackupToolTestCase):
    def test_builds_reference_from_path(self):
        vol = {"id": "uuid-1", "path": "a/b/fir.b.qrd"}
        self.assertEqual(bt.sp_volume_name_from_cs_volume(vol), "~fir.b.qrd")

    def test_missing_path_raises_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "uuid-1"):
            bt.sp_volume_name_from_cs_volume({"id": "uuid-1"})


class TestClusterIdFromGlobalId(BackupToolTestCase):
    def test_three_part_global_id(self):
        self.assertEqual(bt.cluster_id_from_global_id("fir.b.qrd"), "fir.b")

    def test_tilde_prefix_stripped(self):
        self.assertEqual(bt.cluster_id_from_global_id("~fir.b.qrd"), "fir.b")

    def test_plain_volume_name(self):
        self.assertIsNone(bt.cluster_id_from_global_id("myvolume"))

    def test_wrong_token_count(self):
        self.assertIsNone(bt.cluster_id_from_global_id("a.b"))
        self.assertIsNone(bt.cluster_id_from_global_id("a.b.c.d"))


class TestGetClusterForSpVolume(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def test_single_cluster_mode_returns_none(self):
        self.set_config(SC_CONFIG_ALL)
        self.assertIsNone(bt.get_cluster_for_sp_volume("~fir.b.qrd"))

    def test_returns_cluster_id_from_lookup(self):
        bt.sp_api_mc = mock.Mock()
        bt.sp_api_mc.volumeList.return_value = [
            {"clusterId": None}, {"clusterId": "fir.n"},
        ]
        self.assertEqual(bt.get_cluster_for_sp_volume("~fir.b.qrd"), "fir.n")
        bt.sp_api_mc.volumeList.assert_called_once_with(
            "~fir.b.qrd", returnRawAPIData=True
        )

    def test_api_error_falls_back_to_global_id(self):
        bt.sp_api_mc = mock.Mock()
        bt.sp_api_mc.volumeList.side_effect = make_api_error(
            "objectDoesNotExist"
        )
        with self.assertLogs(level="WARNING"):
            self.assertEqual(
                bt.get_cluster_for_sp_volume("~fir.b.qrd"), "fir.b"
            )

    def test_missing_cluster_id_falls_back_to_global_id(self):
        bt.sp_api_mc = mock.Mock()
        bt.sp_api_mc.volumeList.return_value = [{"clusterId": None}]
        self.assertEqual(bt.get_cluster_for_sp_volume("~fir.b.qrd"), "fir.b")

    def test_no_fallback_for_plain_names(self):
        bt.sp_api_mc = mock.Mock()
        bt.sp_api_mc.volumeList.return_value = []
        self.assertIsNone(bt.get_cluster_for_sp_volume("myvolume"))


class TestGetVmClusterName(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def _set_volumes(self, volumes):
        bt.cs_api = mock.Mock()
        bt.cs_api.listVolumes.return_value = {"volume": volumes}

    def test_single_cluster_mode_returns_none(self):
        self.set_config(SC_CONFIG_ALL)
        self.assertIsNone(bt.get_vm_cluster_name("vm-1"))

    def test_resolves_common_cluster(self):
        self._set_volumes([
            {"path": "a/fir.b.qrd"}, {"path": "a/fir.b.qre"},
        ])
        with mock.patch.object(
            bt, "get_cluster_for_sp_volume", return_value="fir.b"
        ) as resolver:
            self.assertEqual(bt.get_vm_cluster_name("vm-1"), "fir.b")
        resolver.assert_any_call("~fir.b.qrd")

    def test_skips_volumes_without_path(self):
        self._set_volumes([{"id": "no-path"}, {"path": "a/fir.n.abc"}])
        with mock.patch.object(
            bt, "get_cluster_for_sp_volume", return_value="fir.n"
        ) as resolver:
            self.assertEqual(bt.get_vm_cluster_name("vm-1"), "fir.n")
        resolver.assert_called_once_with("~fir.n.abc")

    def test_no_volumes_warns_and_returns_none(self):
        self._set_volumes([])
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(bt.get_vm_cluster_name("vm-1"))

    def test_unresolvable_volumes_warn_and_return_none(self):
        self._set_volumes([{"path": "a/fir.b.qrd"}])
        with mock.patch.object(
            bt, "get_cluster_for_sp_volume", return_value=None
        ):
            with self.assertLogs(level="WARNING"):
                self.assertIsNone(bt.get_vm_cluster_name("vm-1"))

    def test_volumes_on_multiple_clusters_raise(self):
        self._set_volumes([{"path": "a/fir.b.qrd"}, {"path": "a/fir.n.abc"}])
        with mock.patch.object(
            bt, "get_cluster_for_sp_volume",
            side_effect=lambda name: bt.cluster_id_from_global_id(name),
        ):
            with self.assertRaisesRegex(RuntimeError, "multiple"):
                bt.get_vm_cluster_name("vm-1")


class TestBackupClusterIds(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def test_all_configured_ids_include_section_overrides(self):
        self.assertEqual(
            bt.all_configured_backup_cluster_ids(),
            {"back.b", "otherback.b"},
        )

    def test_multicluster_union_without_cloudstack_lookup(self):
        # A CloudStack call would explode on cs_api=None -- backups must be
        # discoverable even when the source VM has been deleted.
        self.assertEqual(
            bt.get_backup_cluster_ids(), {"back.b", "otherback.b"}
        )

    def test_single_cluster_uses_the_configured_id(self):
        self.set_config(SC_CONFIG_ALL)
        self.assertEqual(bt.get_backup_cluster_ids(), {"back.b"})


class TestBackupLocationNameFor(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def test_single_cluster_mode_uses_target_settings(self):
        self.set_config(SC_CONFIG_ALL)
        settings = bt.get_cluster_config(None)
        self.assertEqual(
            bt.backup_location_name_for("anything", settings), "backup-loc"
        )

    def test_no_location_id_uses_target_settings(self):
        settings = bt.get_cluster_config("fir.n")
        self.assertEqual(
            bt.backup_location_name_for(None, settings), "backup-loc"
        )

    def test_target_settings_short_circuit(self):
        settings = bt.get_cluster_config("fir.b")
        self.assertEqual(
            bt.backup_location_name_for("otherback.b", settings), "other-loc"
        )

    def test_match_in_globals(self):
        settings = bt.get_cluster_config("fir.b")  # target -> other-loc
        self.assertEqual(
            bt.backup_location_name_for("back.b", settings), "backup-loc"
        )

    def test_match_in_cluster_section(self):
        settings = bt.get_cluster_config("fir.n")  # target -> backup-loc
        self.assertEqual(
            bt.backup_location_name_for("otherback.b", settings), "other-loc"
        )

    def test_no_match_warns_and_falls_back(self):
        settings = bt.get_cluster_config("fir.n")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(
                bt.backup_location_name_for("unknown.x", settings),
                "backup-loc",
            )


class TestGetBackupList(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    VM = "11111111-2222-3333-4444-555555555555"

    def _vc_status(self):
        return [
            {"type": "policy", "id": {"name": "ignored"}},
            {
                "type": "vm",
                "id": {"name": f"cvm={self.VM}"},
                "history": [
                    {"create_ts": 100, "id": {"location": "back.b"}},
                    {"create_ts": 200, "id": {"location": "otherback.b"}},
                    {"create_ts": 300, "id": {"location": "njyf"}},
                ],
            },
        ]

    def _run(self, vm=None, status=None):
        fake = mock.Mock(stdout=json.dumps(
            self._vc_status() if status is None else status
        ))
        with mock.patch.object(
            bt.subprocess, "run", return_value=fake
        ) as run:
            result = bt.get_backup_list(vm or self.VM)
        return result, run.call_args.args[0]

    def test_filters_by_configured_backup_locations(self):
        backups, _ = self._run()
        self.assertEqual(set(backups), {100, 200})

    def test_multicluster_uses_aggregated_status(self):
        _, cmd = self._run()
        self.assertEqual(cmd, ["storpool_vcctl", "status", "-M", "--json"])

    def test_single_cluster_command_and_filter(self):
        self.set_config(SC_CONFIG_ALL)
        backups, cmd = self._run()
        self.assertEqual(cmd, ["storpool_vcctl", "status", "--json"])
        self.assertEqual(set(backups), {100})

    def test_vc_ssh_host_wraps_the_command(self):
        bt.config["VC_SSH_HOST"] = "vc-host"
        _, cmd = self._run()
        self.assertEqual(cmd[:4], ["ssh", "-l", "root", "vc-host"])

    def test_vc_ssh_user_override(self):
        bt.config["VC_SSH_HOST"] = "vc-host"
        bt.config["VC_SSH_USER"] = "operator"
        _, cmd = self._run()
        self.assertEqual(cmd[:4], ["ssh", "-l", "operator", "vc-host"])

    def test_unknown_vm_returns_empty(self):
        backups, _ = self._run(vm="99999999-8888-7777-6666-555555555555")
        self.assertEqual(backups, {})


class TestFixMap(BackupToolTestCase):
    def test_strips_tilde_from_keys_only(self):
        snap_map = {"~uuid-1": "~fir.b.abc", "uuid-2": "~fir.b.abd"}
        bt.fix_map(snap_map)
        self.assertEqual(
            snap_map, {"uuid-1": "~fir.b.abc", "uuid-2": "~fir.b.abd"}
        )


class TestCheckBackupIsUuidFormat(BackupToolTestCase):
    @staticmethod
    def _backup_list(key):
        return {100: {"extra_info": {"sp": {"map": {key: "~fir.b.abc"}}}}}

    def test_uuid_keys_pass(self):
        bt.check_backup_is_uuid_format(
            self._backup_list("11111111-2222-3333-4444-555555555555")
        )

    def test_old_gid_format_raises(self):
        with self.assertRaisesRegex(RuntimeError, "id_tag=uuid"):
            bt.check_backup_is_uuid_format(self._backup_list("fir.b.abc"))


class TestSnapshotFromRemote(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def test_arguments_and_forwarding(self):
        bt.sp_api = mock.Mock()
        settings = bt.get_cluster_config("fir.n")
        bt.snapshot_from_remote("back.b.abc", "fir.n", settings)
        args, kwargs = bt.sp_api.snapshotFromRemote.call_args
        self.assertEqual(args[0], {
            "remoteId": "back.b.abc",
            "remoteLocation": "backup-loc",
            "template": "tpl",
        })
        self.assertEqual(kwargs, {"clusterName": "~fir.n"})

    def test_remote_location_from_backup_entry(self):
        bt.sp_api = mock.Mock()
        settings = bt.get_cluster_config("fir.n")  # would give backup-loc
        bt.snapshot_from_remote(
            "otherback.b.abc", "fir.n", settings, "otherback.b"
        )
        args = bt.sp_api.snapshotFromRemote.call_args.args
        self.assertEqual(args[0]["remoteLocation"], "other-loc")

    def test_existing_copy_is_ok(self):
        bt.sp_api = mock.Mock()
        bt.sp_api.snapshotFromRemote.side_effect = make_api_error(
            "objectExists"
        )
        bt.snapshot_from_remote(
            "back.b.abc", None, bt.get_cluster_config(None)
        )

    def test_other_errors_propagate(self):
        bt.sp_api = mock.Mock()
        bt.sp_api.snapshotFromRemote.side_effect = make_api_error(
            "invalidParam"
        )
        with self.assertRaises(ApiError):
            bt.snapshot_from_remote(
                "back.b.abc", None, bt.get_cluster_config(None)
            )


class TestCopySnapshotsFromRemote(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    def test_strips_tilde_and_passes_location(self):
        volume_list = [{"sp_snapshot": "~fir.b.abc"}]
        settings = bt.get_cluster_config("fir.n")
        with mock.patch.object(bt, "snapshot_from_remote") as pull:
            bt.copy_snapshots_from_remote(
                volume_list, "fir.n", settings, "back.b"
            )
        pull.assert_called_once_with(
            "fir.b.abc", "fir.n", settings, "back.b"
        )


class TestVolumeOperations(BackupToolTestCase):
    config_all = MC_CONFIG_ALL

    VOLUMES = [
        {"sp_volume_name": "~fir.b.qrd", "sp_snapshot": "~fir.b.abc"},
        {"sp_volume_name": "~fir.b.qre", "sp_snapshot": "~fir.b.abd"},
    ]

    def test_detach_volumes(self):
        bt.sp_api = mock.Mock()
        bt.detach_volumes(self.VOLUMES, "fir.b")
        args, kwargs = bt.sp_api.volumesReassignWait.call_args
        self.assertEqual(args[0], {"reassign": [
            {"volume": "~fir.b.qrd", "detach": "all"},
            {"volume": "~fir.b.qre", "detach": "all"},
        ]})
        self.assertEqual(kwargs, {"clusterName": "~fir.b"})

    def test_revert_volumes(self):
        bt.sp_api = mock.Mock()
        bt.revert_volumes(self.VOLUMES, "fir.b")
        bt.sp_api.volumeRevert.assert_any_call(
            "~fir.b.qrd", {"toSnapshot": "~fir.b.abc"}, clusterName="~fir.b"
        )
        self.assertEqual(bt.sp_api.volumeRevert.call_count, 2)

    def test_revert_volumes_with_size(self):
        bt.sp_api = mock.Mock()
        bt.revert_volumes(self.VOLUMES[:1], None, revert_size=True)
        bt.sp_api.volumeRevert.assert_called_once_with(
            "~fir.b.qrd", {"toSnapshot": "~fir.b.abc", "revertSize": True}
        )

    def test_delete_local_snapshots(self):
        bt.sp_api = mock.Mock()
        bt.delete_local_snapshots(self.VOLUMES, "fir.b")
        bt.sp_api.snapshotDelete.assert_any_call(
            "~fir.b.abc", clusterName="~fir.b"
        )
        self.assertEqual(bt.sp_api.snapshotDelete.call_count, 2)


class TestWaitJob(BackupToolTestCase):
    def test_returns_result_when_job_finishes(self):
        bt.cs_api = mock.Mock()
        bt.cs_api.queryAsyncJobResult.side_effect = [
            {"jobstatus": 0, "jobresult": None},
            {"jobstatus": 1, "jobresult": {"ok": True}},
        ]
        with mock.patch.object(bt.time, "sleep"):
            self.assertEqual(bt.wait_job("job-1"), {"ok": True})

    def test_raises_on_timeout(self):
        bt.cs_api = mock.Mock()
        bt.cs_api.queryAsyncJobResult.return_value = {
            "jobstatus": 0, "jobresult": None,
        }
        with mock.patch.object(bt.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Timeout"):
                bt.wait_job("job-1", timeout=3)
        self.assertEqual(bt.cs_api.queryAsyncJobResult.call_count, 3)


if __name__ == "__main__":
    unittest.main()
