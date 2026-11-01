"""pytest options for the contract tests (tests/test_robot_link_contract.py)."""


def pytest_addoption(parser):
    parser.addoption("--target", default=None, metavar="URL",
                     help="run the link contract tests against this server (the real Dell, say http://192.168.50.2:8010) "
                          "instead of tools/fake_base.py; the token is $ROBOT_LINK_TOKEN")
    parser.addoption("--drive", action="store_true", default=False,
                     help="with --target: also run the tests that make the base DRIVE (goals of 0.3 m to 3 m ahead of where it "
                          "stands, a cancel, a heartbeat cut-off). The fake base always runs them")
