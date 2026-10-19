import json
import os
import sys
import time
from pathlib import Path

import requests


ZABBIX_URL = os.environ["ZABBIX_API_URL"]
GRAFANA_URL = os.environ["GRAFANA_URL"].rstrip("/")
GRAFANA_ADMIN_PASSWORD = os.environ["GRAFANA_ADMIN_PASSWORD"]
GRAFANA_ZABBIX_PASSWORD = os.environ["GRAFANA_ZABBIX_PASSWORD"]
SNMP_COMMUNITY = os.environ["SNMP_COMMUNITY"]
TEMPLATE_DIR = Path(os.environ.get("TEMPLATE_DIR", "/templates"))
DASHBOARD_DIR = Path(os.environ.get("DASHBOARD_DIR", "/dashboards"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
TOKEN_FILE = STATE_DIR / "zabbix-api.token"
BOOTSTRAP_TOKEN_NAME = "lab3-bootstrap"
FACTORY_PASSWORD = "zabbix"

HOST_GROUP = "Lab infrastructure"
TEMPLATE_TECH = {
    "rate": "lab-interface-rate",
    "apps": "lab-app-containers",
    "db": "lab-db-container",
}
AGENT_HOST = "docker-host"
SNMP_HOST = "net-device"
LINUX_TEMPLATE = "Linux by Zabbix agent"
SNMP_TEMPLATE = "Generic by SNMP"
RATE_MACRO = "{$IF_RATE_MAX}"
RATE_DEFAULT = "100000000"


class ApiError(RuntimeError):
    pass


def zbx(method, params, token=None, allow_error=False):
    headers = {"Content-Type": "application/json-rpc"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.post(
        ZABBIX_URL,
        headers=headers,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        message = body["error"].get("data") or body["error"].get("message")
        if allow_error:
            return None, message
        raise ApiError(f"{method}: {message}")
    return body["result"], None


def wait_zabbix():
    deadline = time.time() + 480
    while time.time() < deadline:
        try:
            version, _ = zbx("apiinfo.version", [])
            print(f"Zabbix API {version}", flush=True)
            return version
        except Exception as error:
            print(f"waiting for Zabbix API: {error}", flush=True)
            time.sleep(5)
    raise SystemExit("Zabbix API did not become ready")


def login(password):
    result, error = zbx(
        "user.login",
        {"username": "Admin", "password": password},
        allow_error=True,
    )
    if error:
        return None
    return result


def read_saved_token():
    if not TOKEN_FILE.is_file():
        return ""
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def token_works(token):
    if not token:
        return False
    result, error = zbx(
        "user.get",
        {"filter": {"username": ["Admin"]}, "output": ["userid"]},
        token,
        allow_error=True,
    )
    return bool(result) and not error


def write_saved_token(secret):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(secret + "\n", encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def issue_api_token(session):
    users, _ = zbx("user.get", {"filter": {"username": ["Admin"]}, "output": ["userid"]}, session)
    userid = users[0]["userid"]
    found, _ = zbx(
        "token.get",
        {
            "output": ["tokenid"],
            "userids": [userid],
            "filter": {"name": [BOOTSTRAP_TOKEN_NAME]},
        },
        session,
    )
    if found:
        tokenid = found[0]["tokenid"]
    else:
        created, _ = zbx(
            "token.create",
            {
                "name": BOOTSTRAP_TOKEN_NAME,
                "userid": userid,
                "description": "Bootstrap access. Independent of the Admin password.",
                "expires_at": "0",
                "status": "0",
            },
            session,
        )
        tokenid = created["tokenids"][0]
    generated, _ = zbx("token.generate", [tokenid], session)
    secret = generated[0]["token"]
    write_saved_token(secret)
    print("saved API token; later runs do not use the Admin password", flush=True)
    return secret


def ensure_admin_token():
    saved = read_saved_token()
    if token_works(saved):
        print("using saved API token; admin password is left unchanged", flush=True)
        return saved
    session = login(FACTORY_PASSWORD)
    if not session:
        raise SystemExit(
            "Admin password is no longer the factory password, and the saved API token is missing."
        )
    print("using factory Admin password; it is left unchanged", flush=True)
    return issue_api_token(session)


def ensure_group(token):
    found, _ = zbx("hostgroup.get", {"filter": {"name": [HOST_GROUP]}, "output": ["groupid"]}, token)
    if found:
        return found[0]["groupid"]
    created, _ = zbx("hostgroup.create", {"name": HOST_GROUP}, token)
    return created["groupids"][0]


def template_group_id(token):
    found, _ = zbx(
        "templategroup.get",
        {"filter": {"name": ["Templates"]}, "output": ["groupid", "name"]},
        token,
    )
    if found:
        return found[0]["groupid"]
    groups, _ = zbx("templategroup.get", {"output": ["groupid", "name"]}, token)
    if not groups:
        raise SystemExit("no template groups in Zabbix")
    return groups[0]["groupid"]


def stock_template(token, technical_name):
    found, _ = zbx(
        "template.get",
        {"filter": {"host": [technical_name]}, "output": ["templateid", "host", "name"]},
        token,
    )
    if found:
        return found[0]["templateid"]
    sample, _ = zbx(
        "template.get",
        {"search": {"host": technical_name.split()[0]}, "output": ["host", "name"]},
        token,
    )
    names = ", ".join(item["host"] for item in sample[:20])
    raise SystemExit(f"template {technical_name!r} not found. Nearby: {names}")


def ensure_template(token, technical, visible, groupid):
    found, _ = zbx(
        "template.get",
        {"filter": {"host": [technical]}, "output": ["templateid"]},
        token,
    )
    if found:
        return found[0]["templateid"]
    created, _ = zbx(
        "template.create",
        {
            "host": technical,
            "name": visible,
            "groups": [{"groupid": groupid}],
            "description": "Lab 3 custom template",
        },
        token,
    )
    return created["templateids"][0]


def ensure_macro(token, hostid, macro, value):
    found, _ = zbx(
        "usermacro.get",
        {"hostids": [hostid], "filter": {"macro": [macro]}, "output": ["hostmacroid", "value"]},
        token,
    )
    if found:
        if found[0]["value"] != value:
            zbx("usermacro.update", {"hostmacroid": found[0]["hostmacroid"], "value": value}, token)
        return
    zbx(
        "usermacro.create",
        {"hostid": hostid, "macro": macro, "value": value},
        token,
    )


def ensure_item(token, payload):
    found, _ = zbx(
        "item.get",
        {
            "hostids": [payload["hostid"]],
            "filter": {"key_": [payload["key_"]]},
            "output": ["itemid"],
        },
        token,
    )
    if found:
        return found[0]["itemid"]
    created, _ = zbx("item.create", payload, token)
    return created["itemids"][0]


def ensure_trigger(token, description, expression, priority):
    found, _ = zbx(
        "trigger.get",
        {"filter": {"description": [description]}, "output": ["triggerid"]},
        token,
    )
    if found:
        zbx(
            "trigger.update",
            {"triggerid": found[0]["triggerid"], "expression": expression, "priority": priority},
            token,
        )
        return found[0]["triggerid"]
    created, _ = zbx(
        "trigger.create",
        {"description": description, "expression": expression, "priority": priority},
        token,
    )
    return created["triggerids"][0]


def interface(kind, dns, port, community=None):
    data = {
        "type": 1 if kind == "agent" else 2,
        "main": 1,
        "useip": 0,
        "ip": "",
        "dns": dns,
        "port": port,
    }
    if kind == "snmp":
        data["details"] = {
            "version": 2,
            "community": community,
            "bulk": 1,
            "max_repetitions": "10",
        }
    return data


def ensure_host(token, technical, groupid, iface, templates):
    found, _ = zbx(
        "host.get",
        {"filter": {"host": [technical]}, "output": ["hostid"], "selectInterfaces": ["interfaceid", "type"]},
        token,
    )
    if not found:
        created, _ = zbx(
            "host.create",
            {
                "host": technical,
                "groups": [{"groupid": groupid}],
                "interfaces": [iface],
                "templates": [{"templateid": item} for item in templates],
            },
            token,
        )
        return created["hostids"][0]
    hostid = found[0]["hostid"]
    result, error = zbx(
        "host.massadd",
        {"hosts": [{"hostid": hostid}], "templates": [{"templateid": item} for item in templates]},
        token,
        allow_error=True,
    )
    if error and "already" not in error.lower():
        raise ApiError(error)
    return hostid


def link_templates(token, hostid, template_ids):
    result, error = zbx(
        "host.massadd",
        {"hosts": [{"hostid": hostid}], "templates": [{"templateid": item} for item in template_ids]},
        token,
        allow_error=True,
    )
    if error and "already" not in error.lower():
        raise ApiError(error)


def build_rate_template(token, templateid):
    ensure_macro(token, templateid, RATE_MACRO, RATE_DEFAULT)
    master = ensure_item(
        token,
        {
            "hostid": templateid,
            "name": "Incoming octets on interface 2",
            "key_": "net.if.in.octets[2]",
            "type": 20,
            "value_type": 3,
            "delay": "30s",
            "snmp_oid": "1.3.6.1.2.1.31.1.1.1.6.2",
            "units": "B",
        },
    )
    ensure_item(
        token,
        {
            "hostid": templateid,
            "name": "Incoming rate on interface 2",
            "key_": "net.if.in.rate[2]",
            "type": 18,
            "value_type": 0,
            "delay": "0",
            "units": "Bps",
            "master_itemid": master,
            "preprocessing": [{"type": 10, "params": "", "error_handler": 0, "error_handler_params": ""}],
        },
    )
    ensure_trigger(
        token,
        "Interface input rate is above {$IF_RATE_MAX}",
        f"last(/{TEMPLATE_TECH['rate']}/net.if.in.rate[2])>{RATE_MACRO}",
        3,
    )


def build_db_template(token, templateid):
    master = ensure_item(
        token,
        {
            "hostid": templateid,
            "name": "Database container inspect",
            "key_": 'docker.container_info["db",full]',
            "type": 0,
            "value_type": 4,
            "delay": "30s",
            "history": "0",
            "trends": "0",
        },
    )
    status_steps = [
        {"type": 12, "params": "$.State.Status", "error_handler": 0, "error_handler_params": ""},
    ]
    ensure_item(
        token,
        {
            "hostid": templateid,
            "name": "Database container status",
            "key_": 'docker.container_info.state.status["db"]',
            "type": 18,
            "value_type": 1,
            "delay": "0",
            "master_itemid": master,
            "preprocessing": status_steps,
        },
    )
    ensure_item(
        token,
        {
            "hostid": templateid,
            "name": "Lab1 container db is running",
            "key_": 'lab.container.up["db"]',
            "type": 18,
            "value_type": 3,
            "delay": "0",
            "master_itemid": master,
            "preprocessing": status_steps
            + [
                {
                    "type": 21,
                    "params": "return value === 'running' ? 1 : 0;",
                    "error_handler": 0,
                    "error_handler_params": "",
                }
            ],
        },
    )
    ensure_trigger(
        token,
        "Database container is not running",
        f'last(/{TEMPLATE_TECH["db"]}/docker.container_info.state.status["db"])<>"running"',
        5,
    )


def ensure_discovery(token, templateid):
    found, _ = zbx(
        "discoveryrule.get",
        {
            "hostids": [templateid],
            "filter": {"key_": ["docker.containers.discovery"]},
            "output": ["itemid"],
        },
        token,
    )
    if found:
        zbx(
            "discoveryrule.update",
            {
                "itemid": found[0]["itemid"],
                "filter": {
                    "evaltype": 0,
                    "conditions": [{"macro": "{#NAME}", "value": "^/code-app-", "operator": 8}],
                },
            },
            token,
        )
        return found[0]["itemid"]
    created, _ = zbx(
        "discoveryrule.create",
        {
            "name": "Lab1 application containers",
            "key_": "docker.containers.discovery",
            "hostid": templateid,
            "type": 0,
            "delay": "30s",
            "filter": {
                "evaltype": 0,
                "conditions": [{"macro": "{#NAME}", "value": "^/code-app-", "operator": 8}],
            },
        },
        token,
    )
    return created["itemids"][0]


def ensure_item_prototype(token, payload):
    found, _ = zbx(
        "itemprototype.get",
        {
            "discoveryids": [payload["ruleid"]],
            "filter": {"key_": [payload["key_"]]},
            "output": ["itemid"],
        },
        token,
    )
    if found:
        return found[0]["itemid"]
    created, _ = zbx("itemprototype.create", payload, token)
    return created["itemids"][0]


def build_app_template(token, templateid):
    ruleid = ensure_discovery(token, templateid)
    master = ensure_item_prototype(
        token,
        {
            "ruleid": ruleid,
            "hostid": templateid,
            "name": "Container {#NAME} inspect",
            "key_": 'docker.container_info["{#NAME}",full]',
            "type": 0,
            "value_type": 4,
            "delay": "30s",
            "history": "0",
            "trends": "0",
        },
    )
    status_steps = [
        {"type": 12, "params": "$.State.Status", "error_handler": 0, "error_handler_params": ""},
    ]
    ensure_item_prototype(
        token,
        {
            "ruleid": ruleid,
            "hostid": templateid,
            "name": "Container {#NAME} status",
            "key_": 'docker.container_info.state.status["{#NAME}"]',
            "type": 18,
            "value_type": 1,
            "delay": "0",
            "master_itemid": master,
            "preprocessing": status_steps,
        },
    )
    ensure_item_prototype(
        token,
        {
            "ruleid": ruleid,
            "hostid": templateid,
            "name": "Lab1 container {#NAME} is running",
            "key_": 'lab.container.up["{#NAME}"]',
            "type": 18,
            "value_type": 3,
            "delay": "0",
            "master_itemid": master,
            "preprocessing": status_steps
            + [
                {
                    "type": 21,
                    "params": "return value === 'running' ? 1 : 0;",
                    "error_handler": 0,
                    "error_handler_params": "",
                }
            ],
        },
    )
    description = "Application container {#NAME} is not running"
    found, _ = zbx(
        "triggerprototype.get",
        {"discoveryids": [ruleid], "filter": {"description": [description]}, "output": ["triggerid"]},
        token,
    )
    expression = (
        f'last(/{TEMPLATE_TECH["apps"]}/docker.container_info.state.status["{{#NAME}}"])<>"running"'
    )
    if not found:
        zbx(
            "triggerprototype.create",
            {"description": description, "expression": expression, "priority": 4},
            token,
        )


def ensure_web(token, hostid):
    found, _ = zbx(
        "httptest.get",
        {"hostids": [hostid], "filter": {"name": ["lab1-proxy"]}, "output": ["httptestid"]},
        token,
    )
    if not found:
        zbx(
            "httptest.create",
            {
                "name": "lab1-proxy",
                "hostid": hostid,
                "delay": "30s",
                "retries": 1,
                "steps": [
                    {
                        "name": "home",
                        "no": 1,
                        "url": "http://proxy/",
                        "timeout": "10s",
                        "status_codes": "200",
                        "follow_redirects": 1,
                    }
                ],
            },
            token,
        )
    items, _ = zbx(
        "item.get",
        {
            "hostids": [hostid],
            "search": {"key_": "web.test."},
            "output": ["itemid", "key_", "name"],
            "webitems": True,
        },
        token,
    )
    renames = {
        "web.test.rspcode[lab1-proxy,home]": "Lab1 proxy response code",
        "web.test.time[lab1-proxy,home,resp]": "Lab1 proxy response time",
        "web.test.fail[lab1-proxy]": "Lab1 proxy scenario failed",
    }
    for item in items:
        new_name = renames.get(item["key_"])
        if new_name and item["name"] != new_name:
            zbx(
                "item.update",
                {"itemid": item["itemid"], "name": new_name},
                token,
                allow_error=True,
            )
    ensure_trigger(
        token,
        "Application is unavailable via the web proxy",
        f"last(/{AGENT_HOST}/web.test.rspcode[lab1-proxy,home])<>200",
        4,
    )
    by_key = {item["key_"]: item["itemid"] for item in items}
    ensure_item(
        token,
        {
            "hostid": hostid,
            "name": "Lab1 proxy HTTP code",
            "key_": "lab.web.rspcode",
            "type": 18,
            "value_type": 3,
            "delay": "0",
            "history": "31d",
            "trends": "365d",
            "master_itemid": by_key["web.test.rspcode[lab1-proxy,home]"],
        },
    )
    ensure_item(
        token,
        {
            "hostid": hostid,
            "name": "Lab1 proxy HTTP time",
            "key_": "lab.web.time",
            "type": 18,
            "value_type": 0,
            "delay": "0",
            "history": "31d",
            "trends": "365d",
            "units": "s",
            "master_itemid": by_key["web.test.time[lab1-proxy,home,resp]"],
        },
    )


def ensure_interface_status(token, hostid):
    ifaces, _ = zbx(
        "hostinterface.get",
        {"hostids": [hostid], "filter": {"type": 2}, "output": ["interfaceid"]},
        token,
    )
    ensure_item(
        token,
        {
            "hostid": hostid,
            "name": "Interface eth0 operational status",
            "key_": "net.if.status[eth0]",
            "type": 20,
            "value_type": 3,
            "delay": "30s",
            "interfaceid": ifaces[0]["interfaceid"],
            "snmp_oid": "1.3.6.1.2.1.2.2.1.8.2",
            "description": "ifOperStatus.2; 1 means the interface is up",
        },
    )


def ensure_snmp_trigger(token):
    ensure_trigger(
        token,
        "SNMP device is unreachable",
        f"max(/{SNMP_HOST}/zabbix[host,snmp,available],#3)=0",
        4,
    )


def ensure_reader(token, groupid):
    roles, _ = zbx("role.get", {"filter": {"name": ["Grafana reader"]}, "output": ["roleid"]}, token)
    if roles:
        roleid = roles[0]["roleid"]
    else:
        created, _ = zbx(
            "role.create",
            {
                "name": "Grafana reader",
                "type": 1,
                "rules": {
                    "ui.default_access": 1,
                    "modules.default_access": 0,
                    "actions.default_access": 0,
                    "api.access": 1,
                    "api.mode": 0,
                    "api": [],
                },
            },
            token,
        )
        roleid = created["roleids"][0]
    groups, _ = zbx(
        "usergroup.get",
        {"filter": {"name": ["Grafana readers"]}, "output": ["usrgrpid"]},
        token,
    )
    if groups:
        usrgrpid = groups[0]["usrgrpid"]
        zbx(
            "usergroup.update",
            {
                "usrgrpid": usrgrpid,
                "hostgroup_rights": [{"id": groupid, "permission": 2}],
            },
            token,
        )
    else:
        created, _ = zbx(
            "usergroup.create",
            {
                "name": "Grafana readers",
                "hostgroup_rights": [{"id": groupid, "permission": 2}],
            },
            token,
        )
        usrgrpid = created["usrgrpids"][0]
    users, _ = zbx(
        "user.get",
        {"filter": {"username": ["grafana"]}, "output": ["userid"]},
        token,
    )
    if users:
        zbx(
            "user.update",
            {
                "userid": users[0]["userid"],
                "roleid": roleid,
                "passwd": GRAFANA_ZABBIX_PASSWORD,
                "usrgrps": [{"usrgrpid": usrgrpid}],
            },
            token,
        )
        return
    zbx(
        "user.create",
        {
            "username": "grafana",
            "passwd": GRAFANA_ZABBIX_PASSWORD,
            "roleid": roleid,
            "usrgrps": [{"usrgrpid": usrgrpid}],
        },
        token,
    )


def export_templates(token, template_ids):
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    for technical, templateid in template_ids.items():
        exported, _ = zbx(
            "configuration.export",
            {"format": "yaml", "prettyprint": True, "options": {"templates": [templateid]}},
            token,
        )
        path = TEMPLATE_DIR / f"{technical}.yaml"
        path.write_text(exported, encoding="utf-8")
        print(f"exported {path.name}", flush=True)


def wait_grafana():
    deadline = time.time() + 300
    auth = ("admin", GRAFANA_ADMIN_PASSWORD)
    while time.time() < deadline:
        try:
            health = requests.get(f"{GRAFANA_URL}/api/health", timeout=10)
            plugin = requests.get(
                f"{GRAFANA_URL}/api/plugins/alexanderzobnin-zabbix-app/settings",
                auth=auth,
                timeout=10,
            )
            if health.ok and plugin.status_code != 404:
                print(f"Grafana health {health.status_code}, plugin {plugin.status_code}", flush=True)
                return auth
        except Exception as error:
            print(f"waiting for Grafana: {error}", flush=True)
        time.sleep(5)
    raise SystemExit("Grafana did not become ready")


def grafana_request(auth, method, path, payload=None):
    response = requests.request(
        method,
        f"{GRAFANA_URL}{path}",
        auth=auth,
        json=payload,
        timeout=60,
    )
    if response.status_code >= 400:
        raise SystemExit(f"Grafana {method} {path} {response.status_code}: {response.text[:500]}")
    if response.text:
        return response.json()
    return {}


def zabbix_target(ref_id, host_filter, item_filter):
    return {
        "refId": ref_id,
        "datasource": {"type": "alexanderzobnin-zabbix-datasource", "uid": "zabbix"},
        "queryType": "0",
        "group": {"filter": HOST_GROUP},
        "host": {"filter": host_filter},
        "application": {"filter": ""},
        "itemTag": {"filter": ""},
        "item": {"filter": item_filter},
        "functions": [],
        "options": {"showDisabledItems": False, "skipEmptyValues": False},
        "resultFormat": "time_series",
        "table": {"skipEmptyValues": False},
    }


def panel(panel_id, title, x, y, host_filter, item_filter, unit=""):
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
        "datasource": {"type": "alexanderzobnin-zabbix-datasource", "uid": "zabbix"},
        "targets": [zabbix_target("A", host_filter, item_filter)],
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": {"drawStyle": "line", "spanNulls": False}},
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    }


def ensure_grafana(auth):
    grafana_request(
        auth,
        "POST",
        "/api/plugins/alexanderzobnin-zabbix-app/settings",
        {"enabled": True, "pinned": True, "jsonData": None},
    )
    lookup = requests.get(f"{GRAFANA_URL}/api/datasources/uid/zabbix", auth=auth, timeout=30)
    payload = {
        "uid": "zabbix",
        "name": "Zabbix",
        "type": "alexanderzobnin-zabbix-datasource",
        "access": "proxy",
        "url": "http://zabbix-web:8080/api_jsonrpc.php",
        "isDefault": True,
        "jsonData": {"username": "grafana", "trends": True, "cacheTTL": "30s", "timeout": 30},
        "secureJsonData": {"password": GRAFANA_ZABBIX_PASSWORD},
    }
    if lookup.status_code == 404:
        grafana_request(auth, "POST", "/api/datasources", payload)
    elif lookup.ok:
        grafana_request(auth, "PUT", "/api/datasources/uid/zabbix", payload)
    else:
        raise SystemExit(f"datasource lookup failed: {lookup.status_code} {lookup.text[:300]}")
    dashboard = {
        "uid": "lab3",
        "title": "Lab3 infrastructure",
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            panel(1, "Application availability", 0, 0, AGENT_HOST, "Lab1 proxy HTTP code"),
            panel(2, "Application response time", 12, 0, AGENT_HOST, "Lab1 proxy HTTP time", "s"),
            panel(3, "Lab1 container state", 0, 8, AGENT_HOST, "/Lab1 container .* is running/"),
            panel(4, "SNMP interface traffic", 12, 8, SNMP_HOST, "Incoming rate on interface 2", "Bps"),
        ],
    }
    grafana_request(
        auth,
        "POST",
        "/api/dashboards/db",
        {"dashboard": dashboard, "overwrite": True, "folderId": 0},
    )
    saved = grafana_request(auth, "GET", "/api/dashboards/uid/lab3")
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    path = DASHBOARD_DIR / "lab3.json"
    path.write_text(json.dumps(saved["dashboard"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"exported {path.name}", flush=True)


def drop_default_host(token):
    found, _ = zbx("host.get", {"filter": {"host": ["Zabbix server"]}, "output": ["hostid"]}, token)
    if found:
        zbx("host.delete", [found[0]["hostid"]], token)
        print("removed default host Zabbix server", flush=True)


def configure():
    version = wait_zabbix()
    token = ensure_admin_token()
    drop_default_host(token)
    groupid = ensure_group(token)
    tgroup = template_group_id(token)
    linux_id = stock_template(token, LINUX_TEMPLATE)
    snmp_stock_id = stock_template(token, SNMP_TEMPLATE)
    rate_id = ensure_template(token, TEMPLATE_TECH["rate"], "Lab interface rate", tgroup)
    apps_id = ensure_template(token, TEMPLATE_TECH["apps"], "Lab app containers", tgroup)
    db_id = ensure_template(token, TEMPLATE_TECH["db"], "Lab database container", tgroup)
    build_rate_template(token, rate_id)
    build_db_template(token, db_id)
    build_app_template(token, apps_id)
    agent_id = ensure_host(
        token,
        AGENT_HOST,
        groupid,
        interface("agent", "zabbix-agent", "10050"),
        [linux_id, apps_id, db_id],
    )
    link_templates(token, agent_id, [linux_id, apps_id, db_id])
    snmp_id = ensure_host(
        token,
        SNMP_HOST,
        groupid,
        interface("snmp", "net-device", "161", SNMP_COMMUNITY),
        [snmp_stock_id, rate_id],
    )
    link_templates(token, snmp_id, [snmp_stock_id, rate_id])
    ensure_interface_status(token, snmp_id)
    ensure_web(token, agent_id)
    ensure_snmp_trigger(token)
    ensure_reader(token, groupid)
    export_templates(
        token,
        {
            TEMPLATE_TECH["rate"]: rate_id,
            TEMPLATE_TECH["apps"]: apps_id,
            TEMPLATE_TECH["db"]: db_id,
        },
    )
    auth = wait_grafana()
    ensure_grafana(auth)
    print(f"configured Zabbix {version}", flush=True)


def token_or_exit():
    wait_zabbix()
    return ensure_admin_token()


def host_id(token, technical):
    found, _ = zbx("host.get", {"filter": {"host": [technical]}, "output": ["hostid"]}, token)
    if not found:
        raise SystemExit(f"host {technical} not found")
    return found[0]["hostid"]


def set_macro(value):
    token = token_or_exit()
    hostid = host_id(token, SNMP_HOST)
    found, _ = zbx(
        "usermacro.get",
        {"hostids": [hostid], "filter": {"macro": [RATE_MACRO]}, "output": ["hostmacroid"]},
        token,
    )
    if found:
        zbx("usermacro.update", {"hostmacroid": found[0]["hostmacroid"], "value": value}, token)
    else:
        zbx("usermacro.create", {"hostid": hostid, "macro": RATE_MACRO, "value": value}, token)
    print(f"{RATE_MACRO} on {SNMP_HOST} = {value}", flush=True)


def check_now():
    token = token_or_exit()
    rules, _ = zbx(
        "discoveryrule.get",
        {"output": ["itemid", "key_"], "search": {"key_": "docker.containers"}},
        token,
    )
    items, _ = zbx(
        "item.get",
        {
            "output": ["itemid", "key_"],
            "search": {"key_": "docker.container_info"},
            "searchWildcardsEnabled": False,
        },
        token,
    )
    web, _ = zbx(
        "item.get",
        {"output": ["itemid", "key_"], "search": {"key_": "web.test.rspcode"}},
        token,
    )
    snmp, _ = zbx(
        "item.get",
        {"output": ["itemid", "key_"], "filter": {"key_": ["net.if.in.octets[2]"]}},
        token,
    )
    ids = []
    for row in rules + items + web + snmp:
        if row["itemid"] not in ids:
            ids.append(row["itemid"])
    for itemid in ids:
        result, error = zbx(
            "task.create",
            {"type": 6, "request": {"itemid": itemid}},
            token,
            allow_error=True,
        )
        print(itemid, "ok" if not error else error, flush=True)


def show_problems():
    token = token_or_exit()
    problems, _ = zbx(
        "problem.get",
        {"output": "extend", "recent": True, "sortfield": ["eventid"], "sortorder": "DESC"},
        token,
    )
    for problem in problems:
        print(
            f"{problem.get('clock')} {problem.get('r_clock')} sev={problem.get('severity')} {problem.get('name')}",
            flush=True,
        )
    if not problems:
        print("no problems", flush=True)


def show_latest():
    token = token_or_exit()
    items, _ = zbx(
        "item.get",
        {
            "output": ["name", "key_", "lastvalue", "lastclock", "delay", "units"],
            "selectHosts": ["host"],
            "monitored": True,
            "sortfield": "name",
        },
        token,
    )
    interesting = (
        "system.cpu.load",
        "vm.memory.size",
        "vfs.fs.size",
        "net.if.in[",
        "net.if.out[",
        "docker.container",
        "lab.container",
        "web.test.",
        "net.if.in.octets",
        "net.if.in.rate",
        "system.uptime",
        "snmp",
    )
    for item in items:
        key = item["key_"]
        if not any(part in key for part in interesting):
            continue
        host = item["hosts"][0]["host"] if item.get("hosts") else ""
        print(
            f"{host}\t{item['name']}\t{key}\t{item['delay']}\t{item['lastvalue']}\t{item['lastclock']}",
            flush=True,
        )


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "configure"
    if command == "configure":
        configure()
    elif command == "macro":
        set_macro(sys.argv[2])
    elif command == "check-now":
        check_now()
    elif command == "problems":
        show_problems()
    elif command == "latest":
        show_latest()
    else:
        raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    main()
