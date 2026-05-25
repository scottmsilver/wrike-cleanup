import os

import pytest

if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
    pytest.skip("FIRESTORE_EMULATOR_HOST not set", allow_module_level=True)

from google.cloud import firestore
from store import Store


@pytest.fixture
def store():
    db = firestore.Client(project="local-dev")
    # Clean the jobs collection between tests
    for doc in db.collection("jobs").stream():
        doc.reference.delete()
    return Store(db)


def test_create_job_if_absent_inserts_new_doc(store):
    created = store.create_job_if_absent("attA", task_id="T1")
    assert created is True
    doc = store.get_job("attA")
    assert doc["status"] == "pending"
    assert doc["taskId"] == "T1"
    assert doc["attempts"] == 0


def test_create_job_if_absent_does_not_resurrect_done(store):
    store.create_job_if_absent("attB", task_id="T1")
    store.mark_done("attB", preview_id="PA", preview_name="preview_attB.pdf")
    created = store.create_job_if_absent("attB", task_id="T1")
    assert created is False
    doc = store.get_job("attB")
    assert doc["status"] == "done"
    assert doc["previewId"] == "PA"


def test_create_job_if_absent_does_not_resurrect_failed(store):
    store.create_job_if_absent("attC", task_id="T1")
    store.mark_failed("attC", error="boom", error_code="exhausted")
    created = store.create_job_if_absent("attC", task_id="T1")
    assert created is False
    doc = store.get_job("attC")
    assert doc["status"] == "failed"


def test_claim_next_pending_returns_a_job(store):
    store.create_job_if_absent("attA", task_id="T1")
    claimed = store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60)
    assert claimed is not None
    assert claimed["attachmentId"] == "attA"
    assert claimed["leasedBy"] == "worker1"
    assert claimed["attempts"] == 1


def test_claim_skips_already_leased(store):
    store.create_job_if_absent("attA", task_id="T1")
    store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60)
    second = store.claim_next_pending(claimer_id="worker2", lease_ttl_s=60)
    assert second is None


def test_claim_picks_up_expired_lease(store):
    store.create_job_if_absent("attA", task_id="T1")
    store.claim_next_pending(claimer_id="worker1", lease_ttl_s=-1)  # already expired
    second = store.claim_next_pending(claimer_id="worker2", lease_ttl_s=60)
    assert second is not None
    assert second["leasedBy"] == "worker2"
    assert second["attempts"] == 2  # incremented again at re-claim


def test_claim_skips_terminal_states(store):
    store.create_job_if_absent("attA", task_id="T1")
    store.mark_done("attA", preview_id="P1", preview_name="preview_attA.pdf")
    assert store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60) is None


def test_release_lease_for_retry(store):
    store.create_job_if_absent("attA", task_id="T1")
    claimed = store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60)
    store.release_for_retry("attA", next_attempt_in_s=5, error="boom", error_code="soffice_crash")
    job = store.get_job("attA")
    assert job["status"] == "pending"
    assert job["leasedBy"] is None
    assert job["errorCode"] == "soffice_crash"
