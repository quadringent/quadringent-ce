"""Keep first-evaluation answers scoped to the evidence they cite."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_compatibility_distinguishes_release_grants_from_current_source():
    text = (ROOT / 'docs/product/compatibility.md').read_text()
    assert 'v0.4.5' in text
    assert 'fe4db47' in text
    assert 'both history and mirror schemas' in text
    assert 'Verify access' in text
    assert 'Not tested: with the default schemas' not in text

def test_faq_large_copy_uses_dated_million_row_receipt():
    text = (ROOT / 'docs/product/faq.md').read_text()
    evidence = json.loads((ROOT / 'site/qualification-0.4.5.json').read_text())
    closeout = evidence['followup_2026_10_16']
    assert closeout['final_comparison']['source_rows'] == 1_000_000
    assert closeout['final_comparison']['independent_native_journal_images'] == 124
    assert '1,000,000' in text and '124' in text
    assert 'largest published copy is 55' not in text
    assert 'separate qualification harness' in text

def test_faq_does_not_promote_reconciliation_to_endurance_or_sla():
    text = (ROOT / 'docs/product/faq.md').read_text()
    assert '30 of 97' in text
    assert 'not a throughput benchmark' in text
    assert 'not achieved' in text
    assert '2026-10-16' in text

def test_faq_current_version_is_not_old_candidate():
    text = (ROOT / 'docs/product/faq.md').read_text()
    assert '0.4.5 candidate' not in text
    assert 'published 0.4.5 prerelease' in text
    assert 'source version 0.4.9' in text

def test_production_experience_is_distinct_from_release_qualification():
    faq = (ROOT / 'docs/product/faq.md').read_text()
    evidence = (ROOT / 'docs/evidence.md').read_text()
    assert 'maintainer reports repeated tests on production IBM i machines' in faq
    assert 'several million rows' in faq
    assert '| Production use | Not tested |' not in evidence
    assert '| Production use | Maintainer-reported experience |' in evidence
    assert 'production promotion disabled' in evidence
    assert 'not a release-specific measurement' in evidence
