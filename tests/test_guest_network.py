"""Native configuration-key and file-identity rules used by the guest helper."""

from types import SimpleNamespace

import pytest

from rss_aggregator.guest_network import _stamp, _stock_configuration


@pytest.mark.parametrize(
    "config",
    [
        {"network": {"network_config_dir": "/synthetic"}},
        {"Network": {"Network_Config_Dir": "/synthetic"}},
        {"NETWORK": {"NETWORK_CONFIG_DIR": "/synthetic"}},
        {"NetworK": {"Network_Config_Dir": "/synthetic"}},
        {"Engine": {"ENV": ["CONTAINERS_CONF=/synthetic"]}},
        {"engine": {"env": ["CONTAINERS_CONF=/synthetic"]}},
        {"network": {}, "Network": {"network_config_dir": "/synthetic"}},
        {"Network": {"Network_Config_Dir": "/synthetic", "network_config_dir": ""}},
        {"engine": {"env": [], "ENV": ["CONTAINERS_CONF=/synthetic"]}},
    ],
)
def test_native_case_insensitive_config_selectors_and_ambiguous_aliases_refuse(config):
    with pytest.raises(ValueError, match="Unverified network context"):
        _stock_configuration(config)


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"network": {}},
        {"Network": {"Network_Config_Dir": ""}},
        {"ENGINE": {"ENV": []}},
    ],
)
def test_empty_default_configuration_selectors_remain_supported(config):
    _stock_configuration(config)


def test_read_atime_is_not_mistaken_for_configuration_mutation():
    identity = dict(
        st_dev=1, st_ino=2, st_mode=0o100600, st_size=4, st_mtime_ns=5, st_ctime_ns=6
    )
    before = _stamp(SimpleNamespace(**identity, st_atime_ns=7))
    assert _stamp(SimpleNamespace(**identity, st_atime_ns=8)) == before
    assert _stamp(SimpleNamespace(**(identity | {"st_ctime_ns": 9}))) != before
