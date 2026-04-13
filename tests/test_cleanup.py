"""
Тести для cleanup.py

Запуск:
    venv/bin/pytest tests/ -v
"""

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import requests_mock as req_mock_module

from cleanup import (
    _atomic_write,
    _int_cfg,
    date_threshold,
    delete_studies,
    fetch_old_studies,
    format_size,
    GlpiSession,
    cmd_gather,
    cmd_check,
)


# ── format_size ────────────────────────────────────────────────────────────────

class TestFormatSize:
    def test_bytes(self):
        assert format_size(500) == "500.0 Б"

    def test_kilobytes(self):
        assert format_size(1024) == "1.0 КБ"

    def test_megabytes(self):
        assert format_size(1024 ** 2) == "1.0 МБ"

    def test_gigabytes(self):
        assert format_size(1024 ** 3) == "1.0 ГБ"

    def test_terabytes(self):
        assert format_size(1024 ** 4) == "1.0 ТБ"

    def test_petabytes(self):
        assert format_size(1024 ** 5) == "1.0 ПБ"

    def test_fractional(self):
        assert format_size(1536) == "1.5 КБ"

    def test_zero(self):
        assert format_size(0) == "0.0 Б"


# ── date_threshold ─────────────────────────────────────────────────────────────

class TestDateThreshold:
    def test_returns_past_date(self):
        threshold = date_threshold(5)
        assert threshold < datetime.now()

    def test_approximately_n_years_ago(self):
        threshold = date_threshold(5)
        expected_year = datetime.now().year - 5
        assert threshold.year == expected_year

    def test_threshold_is_one_day_before(self):
        """Дослідження рівно N-річної давності не мають потрапляти у вибірку."""
        threshold = date_threshold(1)
        one_year_ago = datetime.now().replace(year=datetime.now().year - 1)
        assert threshold < one_year_ago

    def test_leap_year_feb29(self):
        """29 лютого у невисокосний рік не має падати."""
        with patch("cleanup.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 2, 29, 12, 0, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # 2024 - 5 = 2019 (не високосний) — має повернути 28 лютого
            result = date_threshold(5)
            assert result.month == 2
            assert result.day in (27, 28)


# ── _int_cfg ───────────────────────────────────────────────────────────────────

class TestIntCfg:
    def test_valid_integer(self):
        with patch.dict(os.environ, {"TEST_VAR": "42"}):
            assert _int_cfg("TEST_VAR", "0") == 42

    def test_default_value(self):
        os.environ.pop("TEST_VAR", None)
        assert _int_cfg("TEST_VAR", "10") == 10

    def test_invalid_value_exits(self):
        with patch.dict(os.environ, {"TEST_VAR": "abc"}):
            with pytest.raises(SystemExit):
                _int_cfg("TEST_VAR", "0")


# ── _atomic_write ──────────────────────────────────────────────────────────────

class TestAtomicWrite:
    def test_creates_file(self, tmp_path):
        f = tmp_path / "test.json"
        _atomic_write(f, '{"key": "value"}')
        assert f.exists()
        assert json.loads(f.read_text()) == {"key": "value"}

    def test_no_tmp_file_left(self, tmp_path):
        f = tmp_path / "test.json"
        _atomic_write(f, "data")
        assert not (tmp_path / "test.tmp").exists()

    def test_overwrites_existing(self, tmp_path):
        f = tmp_path / "test.json"
        _atomic_write(f, "old")
        _atomic_write(f, "new")
        assert f.read_text() == "new"


# ── fetch_old_studies ──────────────────────────────────────────────────────────

class TestFetchOldStudies:
    BASE = "http://orthanc-test:8042"

    def _config(self, session):
        return {
            "orthanc_url":        self.BASE,
            "orthanc_user":       "orthanc",
            "orthanc_password":   "orthanc",
            "orthanc_verify_ssl": False,
            "retention_years":    5,
            "session":            session,
        }

    def test_returns_studies(self, requests_mock):
        requests_mock.post(f"{self.BASE}/tools/find", json=[
            {
                "ID": "abc-123",
                "PatientMainDicomTags": {"PatientName": "Іваненко^Іван", "PatientID": "P001"},
                "MainDicomTags": {"StudyDate": "20150101", "StudyDescription": "XR Chest"},
            }
        ])
        import requests as req
        c = self._config(req.Session())
        studies = fetch_old_studies(c)
        assert len(studies) == 1
        assert studies[0]["orthanc_id"] == "abc-123"
        assert studies[0]["patient_name"] == "Іваненко^Іван"
        assert studies[0]["study_date"] == "20150101"

    def test_empty_result(self, requests_mock):
        requests_mock.post(f"{self.BASE}/tools/find", json=[])
        import requests as req
        studies = fetch_old_studies(self._config(req.Session()))
        assert studies == []

    def test_missing_tags_default_to_empty_string(self, requests_mock):
        requests_mock.post(f"{self.BASE}/tools/find", json=[
            {"ID": "xyz", "PatientMainDicomTags": {}, "MainDicomTags": {}}
        ])
        import requests as req
        studies = fetch_old_studies(self._config(req.Session()))
        assert studies[0]["patient_name"] == ""
        assert studies[0]["study_date"] == ""

    def test_raises_on_http_error(self, requests_mock):
        requests_mock.post(f"{self.BASE}/tools/find", status_code=500)
        import requests as req
        with pytest.raises(requests.HTTPError):
            fetch_old_studies(self._config(req.Session()))


# ── delete_studies ─────────────────────────────────────────────────────────────

class TestDeleteStudies:
    BASE = "http://orthanc-test:8042"

    def _config(self, session):
        return {
            "orthanc_url":        self.BASE,
            "orthanc_user":       "orthanc",
            "orthanc_password":   "orthanc",
            "orthanc_verify_ssl": False,
            "session":            session,
        }

    STUDY = {"orthanc_id": "abc-123", "patient_name": "Тест", "study_date": "20150101"}

    def test_successful_delete(self, requests_mock):
        requests_mock.get(f"{self.BASE}/studies/abc-123/statistics", json={"DiskSize": 1024 ** 2})
        requests_mock.delete(f"{self.BASE}/studies/abc-123", status_code=200)
        import requests as req
        deleted, skipped, failed, freed = delete_studies(self._config(req.Session()), [self.STUDY])
        assert deleted == 1
        assert skipped == 0
        assert failed == []
        assert freed == 1024 ** 2

    def test_already_deleted_404(self, requests_mock):
        requests_mock.get(f"{self.BASE}/studies/abc-123/statistics", status_code=404)
        requests_mock.delete(f"{self.BASE}/studies/abc-123", status_code=404)
        import requests as req
        deleted, skipped, failed, freed = delete_studies(self._config(req.Session()), [self.STUDY])
        assert deleted == 0
        assert skipped == 1
        assert freed == 0

    def test_network_error_adds_to_failed(self, requests_mock):
        requests_mock.get(f"{self.BASE}/studies/abc-123/statistics", json={"DiskSize": 0})
        requests_mock.delete(f"{self.BASE}/studies/abc-123", exc=requests.ConnectionError("timeout"))
        import requests as req
        deleted, skipped, failed, freed = delete_studies(self._config(req.Session()), [self.STUDY])
        assert deleted == 0
        assert len(failed) == 1
        assert failed[0]["orthanc_id"] == "abc-123"

    def test_statistics_failure_does_not_block_delete(self, requests_mock):
        """Помилка /statistics не має зупиняти видалення."""
        requests_mock.get(f"{self.BASE}/studies/abc-123/statistics", exc=requests.ConnectionError)
        requests_mock.delete(f"{self.BASE}/studies/abc-123", status_code=200)
        import requests as req
        deleted, skipped, failed, freed = delete_studies(self._config(req.Session()), [self.STUDY])
        assert deleted == 1
        assert freed == 0  # розмір невідомий — не враховується

    def test_multiple_studies_continues_after_error(self, requests_mock):
        """При помилці одного дослідження решта мають продовжити видалятись."""
        s1 = {"orthanc_id": "aaa", "patient_name": "А", "study_date": "20150101"}
        s2 = {"orthanc_id": "bbb", "patient_name": "Б", "study_date": "20150102"}
        requests_mock.get(f"{self.BASE}/studies/aaa/statistics", json={"DiskSize": 0})
        requests_mock.get(f"{self.BASE}/studies/bbb/statistics", json={"DiskSize": 0})
        requests_mock.delete(f"{self.BASE}/studies/aaa", exc=requests.ConnectionError)
        requests_mock.delete(f"{self.BASE}/studies/bbb", status_code=200)
        import requests as req
        deleted, skipped, failed, freed = delete_studies(self._config(req.Session()), [s1, s2])
        assert deleted == 1
        assert len(failed) == 1


# ── GlpiSession ────────────────────────────────────────────────────────────────

class TestGlpiSession:
    BASE = "http://glpi-test"

    def _config(self):
        return {
            "glpi_url":            self.BASE,
            "glpi_app_token":      "app-token",
            "glpi_user_token":     "user-token",
            "glpi_verify_ssl":     False,
            "retention_years":     5,
            "server_name":         "xr",
            "glpi_category_id":    1,
            "glpi_entity_id":      0,
            "glpi_assign_user_id": 4,
        }

    def test_init_session(self, requests_mock):
        requests_mock.get(f"{self.BASE}/apirest.php/initSession",
                          json={"session_token": "tok-123"})
        requests_mock.get(f"{self.BASE}/apirest.php/killSession", json={})
        with GlpiSession(self._config()) as g:
            assert g.token == "tok-123"

    def test_init_session_missing_token_raises(self, requests_mock):
        requests_mock.get(f"{self.BASE}/apirest.php/initSession", json={"error": "unauthorized"})
        requests_mock.get(f"{self.BASE}/apirest.php/killSession", json={})
        with pytest.raises(ValueError, match="session_token"):
            with GlpiSession(self._config()):
                pass

    def test_exit_without_token_does_not_raise(self):
        """__exit__ при token=None не має падати."""
        g = GlpiSession(self._config())
        g.__exit__(None, None, None)  # token is None

    def test_get_ticket_status(self, requests_mock):
        requests_mock.get(f"{self.BASE}/apirest.php/initSession",
                          json={"session_token": "tok"})
        requests_mock.get(f"{self.BASE}/apirest.php/Ticket/42", json={"status": 5})
        requests_mock.get(f"{self.BASE}/apirest.php/killSession", json={})
        with GlpiSession(self._config()) as g:
            assert g.get_ticket_status(42) == 5

    def test_get_ticket_status_missing_key_raises(self, requests_mock):
        requests_mock.get(f"{self.BASE}/apirest.php/initSession",
                          json={"session_token": "tok"})
        requests_mock.get(f"{self.BASE}/apirest.php/Ticket/42", json={"id": 42})
        requests_mock.get(f"{self.BASE}/apirest.php/killSession", json={})
        with GlpiSession(self._config()) as g:
            with pytest.raises(ValueError, match="status"):
                g.get_ticket_status(42)


# ── cmd_gather ─────────────────────────────────────────────────────────────────

class TestCmdGather:
    ORTHANC = "http://orthanc-test:8042"
    GLPI    = "http://glpi-test"

    def _config(self, tmp_path, session):
        return {
            "orthanc_url":         self.ORTHANC,
            "orthanc_user":        "orthanc",
            "orthanc_password":    "orthanc",
            "orthanc_verify_ssl":  False,
            "retention_years":     5,
            "glpi_url":            self.GLPI,
            "glpi_app_token":      "app",
            "glpi_user_token":     "user",
            "glpi_verify_ssl":     False,
            "glpi_category_id":    1,
            "glpi_entity_id":      0,
            "glpi_assign_user_id": 4,
            "server_name":         "xr",
            "studies_file":        tmp_path / "studies.json",
            "state_file":          tmp_path / "state.json",
            "session":             session,
        }

    def test_gather_creates_state_and_studies(self, requests_mock, tmp_path):
        requests_mock.post(f"{self.ORTHANC}/tools/find", json=[
            {"ID": "s1", "PatientMainDicomTags": {"PatientName": "Тест", "PatientID": "P1"},
             "MainDicomTags": {"StudyDate": "20150101", "StudyDescription": "XR"}}
        ])
        requests_mock.get(f"{self.GLPI}/apirest.php/initSession",
                          json={"session_token": "tok"})
        requests_mock.post(f"{self.GLPI}/apirest.php/Ticket", json={"id": 99})
        requests_mock.get(f"{self.GLPI}/apirest.php/killSession", json={})

        import requests as req
        c = self._config(tmp_path, req.Session())
        cmd_gather(c)

        assert c["studies_file"].exists()
        assert c["state_file"].exists()
        state = json.loads(c["state_file"].read_text())
        assert state["ticket_id"] == 99

    def test_gather_exits_if_state_exists(self, tmp_path):
        import requests as req
        c = self._config(tmp_path, req.Session())
        c["state_file"].parent.mkdir(parents=True, exist_ok=True)
        c["state_file"].write_text('{"ticket_id": 1}')
        with pytest.raises(SystemExit):
            cmd_gather(c)

    def test_gather_no_studies_does_nothing(self, requests_mock, tmp_path):
        requests_mock.post(f"{self.ORTHANC}/tools/find", json=[])
        import requests as req
        c = self._config(tmp_path, req.Session())
        cmd_gather(c)
        assert not c["state_file"].exists()


# ── cmd_check ──────────────────────────────────────────────────────────────────

class TestCmdCheck:
    ORTHANC = "http://orthanc-test:8042"
    GLPI    = "http://glpi-test"

    def _config(self, tmp_path, session):
        return {
            "orthanc_url":         self.ORTHANC,
            "orthanc_user":        "orthanc",
            "orthanc_password":    "orthanc",
            "orthanc_verify_ssl":  False,
            "glpi_url":            self.GLPI,
            "glpi_app_token":      "app",
            "glpi_user_token":     "user",
            "glpi_verify_ssl":     False,
            "glpi_category_id":    1,
            "glpi_entity_id":      0,
            "glpi_assign_user_id": 4,
            "server_name":         "xr",
            "studies_file":        tmp_path / "studies.json",
            "state_file":          tmp_path / "state.json",
            "session":             session,
        }

    def test_check_no_state_does_nothing(self, tmp_path):
        import requests as req
        c = self._config(tmp_path, req.Session())
        cmd_check(c)  # не має кидати виняток

    def test_check_ticket_not_approved(self, requests_mock, tmp_path):
        requests_mock.get(f"{self.GLPI}/apirest.php/initSession",
                          json={"session_token": "tok"})
        requests_mock.get(f"{self.GLPI}/apirest.php/Ticket/7", json={"status": 1})
        requests_mock.get(f"{self.GLPI}/apirest.php/killSession", json={})

        import requests as req
        c = self._config(tmp_path, req.Session())
        c["state_file"].parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(c["state_file"], json.dumps({"ticket_id": 7, "created_at": "2026-01-01"}))

        cmd_check(c)
        assert c["state_file"].exists()  # не видалений — ще не погоджено

    def test_check_approved_deletes_studies(self, requests_mock, tmp_path):
        requests_mock.get(f"{self.GLPI}/apirest.php/initSession",
                          json={"session_token": "tok"})
        requests_mock.get(f"{self.GLPI}/apirest.php/Ticket/7", json={"status": 5})
        requests_mock.post(f"{self.GLPI}/apirest.php/ITILFollowup", json={"id": 1})
        requests_mock.get(f"{self.GLPI}/apirest.php/killSession", json={})
        requests_mock.get(f"{self.ORTHANC}/studies/s1/statistics", json={"DiskSize": 1024})
        requests_mock.delete(f"{self.ORTHANC}/studies/s1", status_code=200)

        import requests as req
        c = self._config(tmp_path, req.Session())
        c["state_file"].parent.mkdir(parents=True, exist_ok=True)
        studies = [{"orthanc_id": "s1", "patient_name": "Тест", "study_date": "20150101"}]
        _atomic_write(c["studies_file"], json.dumps(studies))
        _atomic_write(c["state_file"], json.dumps({"ticket_id": 7, "created_at": "2026-01-01"}))

        cmd_check(c)
        assert not c["state_file"].exists()
        assert not c["studies_file"].exists()

    def test_check_failed_saves_failed_json(self, requests_mock, tmp_path):
        requests_mock.get(f"{self.GLPI}/apirest.php/initSession",
                          json={"session_token": "tok"})
        requests_mock.get(f"{self.GLPI}/apirest.php/Ticket/7", json={"status": 5})
        requests_mock.post(f"{self.GLPI}/apirest.php/ITILFollowup", json={"id": 1})
        requests_mock.get(f"{self.GLPI}/apirest.php/killSession", json={})
        requests_mock.get(f"{self.ORTHANC}/studies/s1/statistics", json={"DiskSize": 0})
        requests_mock.delete(f"{self.ORTHANC}/studies/s1",
                             exc=requests.ConnectionError("fail"))

        import requests as req
        c = self._config(tmp_path, req.Session())
        c["state_file"].parent.mkdir(parents=True, exist_ok=True)
        studies = [{"orthanc_id": "s1", "patient_name": "Тест", "study_date": "20150101"}]
        _atomic_write(c["studies_file"], json.dumps(studies))
        _atomic_write(c["state_file"], json.dumps({"ticket_id": 7, "created_at": "2026-01-01"}))

        cmd_check(c)
        failed_file = c["studies_file"].with_name("failed_studies.json")
        assert failed_file.exists()
        assert len(json.loads(failed_file.read_text())) == 1
