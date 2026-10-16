import json
import subprocess

import pytest

from tests.javascript_helpers import APP_JS, NODE, extract_javascript_function


pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")


def run_javascript(names, body):
    source = APP_JS.read_text(encoding="utf-8")
    functions = []
    for name in names:
        function = extract_javascript_function(source, name)
        if f"async function {name}(" in source:
            function = "async " + function
        functions.append(function)
    result = subprocess.run(
        [NODE, "-"], input="\n".join(functions) + body, text=True, capture_output=True, check=True
    )
    return json.loads(result.stdout)


HARNESS = """
const button = {disabled: false};
const status = {
    classList: {
        classes: new Set(),
        add(c) { this.classes.add(c); },
        remove(c) { this.classes.delete(c); }
    },
    textContent: ''
};
const document = {getElementById: id =>
    id === 'steam-refresh-btn' ? button : (id === 'steam-refresh-status' ? status : null)};
const state = {settings: {steam_configured: true}, translations: {gui: {settings: {}}}};
function steamErrorText(code) { return 'ERR:' + code; }
let updated = null;
function updateSettingsUI(settings) { updated = settings; }
let calls = 0, requests = [], responses = [];
function fetch(url, options) {
    calls++;
    requests.push({url, options});
    return Promise.resolve({json: async () => responses.shift()});
}
"""


def test_refresh_steam_now_posts_and_reports_added_games():
    result = run_javascript(["refreshSteamNow"], HARNESS + """
(async () => {
    responses = [{success: true, added: 2, matched: ['A', 'B'], owned_count: 5,
                  settings: {steam_owned_count: 5}}];
    await refreshSteamNow();
    process.stdout.write(JSON.stringify({
        text: status.textContent, disabled: button.disabled, calls, requests, updated,
        error: status.classList.classes.has('error')
    }));
})();
""")
    assert result["text"] == "Check complete: added 2 new game(s) to Games to Watch."
    assert result["disabled"] is False
    assert result["calls"] == 1
    assert result["requests"] == [{"url": "/api/steam/refresh", "options": {"method": "POST"}}]
    assert result["updated"] == {"steam_owned_count": 5}
    assert result["error"] is False


def test_refresh_steam_now_handles_no_matches_error_and_unconfigured():
    result = run_javascript(["refreshSteamNow"], HARNESS + """
(async () => {
    responses = [{success: true, added: 0, matched: []}];
    await refreshSteamNow();
    const noMatches = status.textContent;

    responses = [{success: true, added: 0, matched: ['A']}];
    await refreshSteamNow();
    const noneNew = status.textContent;

    responses = [{success: false, error: 'steam_unreachable'}];
    await refreshSteamNow();
    const errorText = status.textContent;
    const errorFlag = status.classList.classes.has('error');
    const callsAfterError = calls;

    state.settings.steam_configured = false;
    await refreshSteamNow();

    process.stdout.write(JSON.stringify({
        noMatches, noneNew, errorText, errorFlag, callsAfterError, finalCalls: calls,
        unconfigured: status.textContent, unconfiguredError: status.classList.classes.has('error')
    }));
})();
""")
    assert result["noMatches"] == "Check complete: none of your owned games currently have Twitch drops."
    assert result["noneNew"] == "Check complete: no new games to add."
    assert result["errorText"] == "ERR:steam_unreachable"
    assert result["errorFlag"] is True
    assert result["callsAfterError"] == 3
    assert result["finalCalls"] == 3
    assert result["unconfigured"] == 'Link Steam first with "Import from Steam".'
    assert result["unconfiguredError"] is False
