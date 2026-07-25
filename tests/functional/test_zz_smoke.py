def test_health(client, storage):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert storage._get_conn.called

def test_dashboard(web_client, storage):
    storage.users.get_dashboard_data.return_value = {
        "stats": {"searches": 1, "total_listings": 2, "new_today": 0},
        "searches": [],
        "recent": [],
    }
    resp = web_client.get("/dashboard")
    print(resp.status_code)
    assert resp.status_code == 200
