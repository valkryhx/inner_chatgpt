from app.acp.config import load_acp_settings


def test_disabled_without_command():
    s = load_acp_settings({})
    assert s.enabled is False
    assert s.command == []


def test_python_module_command():
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": r"C:\anaconda3\python.exe"})
    assert s.enabled is True
    assert s.command == [r"C:\anaconda3\python.exe", "-m",
                         "frontends.acp.genericagent_acp_bridge"]


def test_explicit_json_command_wins():
    s = load_acp_settings({
        "INNER_ACP_GA_PYTHON": "ignored",
        "INNER_ACP_GA_COMMAND": '["C:/ga/python.exe","-m","x"]',
    })
    assert s.command == ["C:/ga/python.exe", "-m", "x"]


def test_defaults():
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": "python"})
    assert (s.mode, s.permission, s.workdir_mode) == ("full_access", "allow", "workspace")
    assert (s.max_active_user, s.max_active_global, s.max_open_user) == (4, 80, 8)
    assert (s.prompt_timeout_s, s.drain_timeout_s, s.idle_ttl_s) == (1800.0, 300.0, 600.0)


def test_env_allow_parsed():
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": "python", "INNER_ACP_ENV_ALLOW": "FOO, BAR"})
    assert s.env_allow == ("FOO", "BAR")


def test_child_env_whitelist_and_proxy():
    from app.acp.config import build_child_env
    base = {"PATH": "p", "APPDATA": "a", "LOCALAPPDATA": "l", "HTTPS_PROXY": "proxy",
            "INNER_SECRET": "no", "GA_MCP_CONFIG": "m.json"}
    env = build_child_env(load_acp_settings({"INNER_ACP_GA_PYTHON": "python"}), base)
    assert env["APPDATA"] == "a"
    assert env["HTTPS_PROXY"] == "proxy"
    assert "INNER_SECRET" not in env
    assert env["GA_MCP_CONFIG"] == "m.json"


def test_child_env_allow_list_and_mcp_override():
    from app.acp.config import build_child_env
    s = load_acp_settings({"INNER_ACP_GA_PYTHON": "python",
                           "INNER_ACP_ENV_ALLOW": "FOO",
                           "INNER_ACP_GA_MCP_CONFIG": "empty.json"})
    env = build_child_env(s, {"FOO": "1", "BAR": "2"})
    assert env["FOO"] == "1"
    assert "BAR" not in env
    assert env["GA_MCP_CONFIG"] == "empty.json"
