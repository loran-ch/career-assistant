from career_assistant.share import token_digest


def test_api_requires_login(client):
    response = client.get("/api/v1/board")
    assert response.status_code == 401
    assert response.get_json()["ok"] is False


def test_board_groups_applications_by_status(auth_client, store, seed_job):
    _, snapshot_id = seed_job()
    store.application_for_job(snapshot_id, "applied", note="已投递")
    response = auth_client.get("/api/v1/board")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    columns = {column["status"]: column for column in payload["board"]["columns"]}
    assert columns["applied"]["count"] == 1
    assert columns["applied"]["cards"][0]["title"] == "数据分析师"
    assert payload["summary"]["applications_total"] == 1


def test_applications_filter_by_status_and_keyword(auth_client, store, seed_job):
    _, first = seed_job(title="数据分析师", company="示例科技", uid="seed-1")
    _, second = seed_job(title="后端工程师", company="另一家", uid="seed-2")
    store.application_for_job(first, "applied")
    store.application_for_job(second, "saved")
    applied = auth_client.get("/api/v1/applications?status=applied").get_json()
    assert applied["page"]["total"] == 1
    assert applied["items"][0]["title"] == "数据分析师"
    matched = auth_client.get("/api/v1/applications?q=后端").get_json()
    assert matched["page"]["total"] == 1
    assert matched["items"][0]["company"] == "另一家"
    empty = auth_client.get("/api/v1/applications?status=offer").get_json()
    assert empty["page"]["total"] == 0


def test_share_round_trip(auth_client, store, csrf, resume):
    report_id = store.save_report(resume["id"], {"facts": [], "audit_items": []})
    created = auth_client.post(
        f"/api/v1/reports/{report_id}/share",
        json={"ttl_days": 3},
        headers={"X-CSRF": csrf},
    )
    assert created.status_code == 200
    payload = created.get_json()
    token = payload["token"]
    assert payload["url"].endswith(f"/api/v1/share/{token}")
    assert payload["summary"]["audit_total"] == 0

    public = auth_client.get(f"/api/v1/share/{token}")
    assert public.status_code == 200
    assert public.get_json()["ok"] is True

    listed = auth_client.get("/api/v1/shares").get_json()
    assert listed["items"][0]["token_hint"] == token[:8]
    assert token not in str(listed)

    deleted = auth_client.delete(f"/api/v1/share/{token}", headers={"X-CSRF": csrf})
    assert deleted.status_code == 200
    assert auth_client.get(f"/api/v1/share/{token}").status_code == 404


def test_mutating_api_requires_csrf(auth_client, store, resume):
    report_id = store.save_report(resume["id"], {"facts": [], "audit_items": []})
    response = auth_client.post(f"/api/v1/reports/{report_id}/share", json={})
    assert response.status_code == 403


def test_share_of_unknown_report_is_not_found(auth_client, csrf):
    response = auth_client.post("/api/v1/reports/999/share", json={},
                                headers={"X-CSRF": csrf})
    assert response.status_code == 404


def test_unknown_share_token_is_not_found(auth_client):
    response = auth_client.get("/api/v1/share/not-a-real-token")
    assert response.status_code == 404


def test_share_token_is_stored_hashed(auth_client, store, csrf, resume):
    report_id = store.save_report(resume["id"], {"facts": [], "audit_items": []})
    token = auth_client.post(
        f"/api/v1/reports/{report_id}/share", json={}, headers={"X-CSRF": csrf}
    ).get_json()["token"]
    record = store.get_share_token(token_digest(token))
    assert record is not None
    assert record["token_hash"] != token
