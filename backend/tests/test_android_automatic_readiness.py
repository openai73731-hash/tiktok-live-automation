"""One-button production preparation, real SDK binding and SQLite; fake OS/SDK only."""
import pytest
from TikTokLive.events import DisconnectEvent

from test_mobile_account_challenge import mobile, OWN, until
from test_manual_send_rc import ACCOUNT, xml


async def own_profile(m):
    m.phone.snapshots = [xml('profile').replace('@'+ACCOUNT, '@'+OWN)]
    result = await m.http.post('/api/admin/mobile/readiness', json={})
    assert result.status_code == 200, result.text
    assert m.service.account_proof.prepared_now()
    assert m.service.account_proof.profile_binding == m.service.account_proof.prepared
    assert m.service.controller.last.account_verified
    assert m.service.account_proof.challenge is None and not m.service.account_proof.valid()
    assert m.service.mode == 'MANUAL' and not m.service.account_proof.manual_accepted
    return result.json()


async def connect_host(m):
    m.phone.snapshots = [xml('live_idle')]
    result = await m.http.post('/api/admin/live/connect', json={'username':OWN})
    assert result.status_code == 202, result.text
    await until(lambda: m.service.account_proof.scope() is not None and not m.service.tasks)


async def test_one_action_checks_exact_profile_then_connect_binds_account_without_phrase(mobile):
    m = mobile
    result = await own_profile(m)
    assert next(c for c in result['checks'] if c['name']=='Свой аккаунт')['ok']
    assert not result['ready'] and not result['auto_enabled']
    await connect_host(m)
    assert m.service.account_proof.valid() and m.service.controller.last.live_verified
    assert m.service.account_proof.challenge is None
    assert m.service.mode == 'MANUAL' and not m.service.account_proof.manual_accepted
    assert m.service.journal.connection.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert not m.app.state.admin.live_history


@pytest.mark.parametrize('profile', ['foreign', 'similar_handle', 'display_only', 'host_only', 'unavailable'])
async def test_no_identity_from_similar_name_display_or_host_without_own_profile(mobile, profile):
    m = mobile
    text = xml('profile').replace('@'+ACCOUNT, '@'+OWN)
    if profile == 'foreign': text = text.replace('@'+OWN, '@another_owner')
    elif profile == 'similar_handle': text = text.replace('@'+OWN, '@'+OWN+'2')
    elif profile == 'display_only': text = text.replace(':id/sxq', ':id/unknown_owner_control')
    elif profile == 'host_only': text = xml('live_idle')
    elif profile == 'unavailable': text = 'idle'
    m.phone.snapshots = [text]
    result = await m.http.post('/api/admin/mobile/readiness', json={})
    assert result.status_code == (409 if profile == 'display_only' else 200), result.text
    assert not m.service.account_proof.profile_binding
    await connect_host(m)
    m.clients[-1].comment('hello')
    assert not m.service.account_proof.valid() and not m.service.controller.last.account_verified
    assert m.service.account_proof.challenge is None and m.service.mode == 'MANUAL'


@pytest.mark.parametrize('change', ['foreign_profile', 'installation', 'security', 'missing_hierarchy'])
async def test_automatic_profile_binding_revokes_on_fresh_adverse_evidence(mobile, change):
    m = mobile
    await own_profile(m)
    await connect_host(m)
    assert m.service.account_proof.valid()
    m.phone.snapshots = [xml('live_idle')]
    if change == 'foreign_profile': m.phone.snapshots = [xml('profile').replace('@'+ACCOUNT, '@foreign')]
    elif change == 'installation': m.phone.info = m.phone.info.replace('10:00:00','11:00:00')
    elif change == 'security': m.phone.snapshots = [xml('live_idle').replace('text=""','text="captcha"',1)]
    elif change == 'missing_hierarchy': m.phone.snapshots = ['idle']
    result = await m.http.post('/api/admin/device/inspect',json={})
    assert result.status_code == 200
    assert not m.service.account_proof.valid() and not result.json()['inspection']['account_verified']
    assert m.service.mode == 'MANUAL'


async def test_reconnect_cannot_reuse_stale_profile_authority_or_auto_issue_a_phrase(mobile):
    m = mobile
    await own_profile(m)
    await connect_host(m)
    old = m.clients[-1]
    old.handlers[DisconnectEvent](None)
    old.closed.set()
    await until(lambda: len(m.clients)==2 and m.service.account_proof.scope() is not None and not m.service.tasks)
    assert not m.service.account_proof.valid()
    assert not m.service.controller.last.account_verified
    assert m.service.account_proof.challenge is None and m.service.mode == 'MANUAL'


async def test_browser_cannot_supply_automatic_account_proof(mobile):
    m = mobile
    result = await m.http.post('/api/admin/mobile/readiness',json={'account_verified':True,'profile_binding':'forged'})
    assert result.status_code == 422
    assert m.service.account_proof.profile_binding is None and not m.service.controller.last.account_verified


async def test_runtime_change_during_host_read_does_not_bind_profile_to_new_runtime(mobile, monkeypatch):
    m = mobile
    await own_profile(m)
    inspect = m.service.controller.inspect
    async def changed(target):
        state = await inspect(target)
        m.runtime.active_runtime_revision = 'f'*32
        return state
    monkeypatch.setattr(m.service.controller, 'inspect', changed)
    await connect_host(m)
    assert not m.service.account_proof.valid()
    assert m.service.account_proof.challenge is None and m.service.mode == 'MANUAL'
