from copy import deepcopy
import pytest

from quadringent_control_plane.projection import project_console_document
from test_bound_proof_windows import BINDING, BOOTSTRAP, IDENTITY
from test_window_delivery_projection import evidence, fresh_running_document, NOW, LIVE_SOURCE


def bound_document():
    document = fresh_running_document()
    value = evidence()
    window = value['window']
    window['format_version'] = 'quadringent-closed-window-v3'
    window['intent'].update(format_version='quadringent-window-intent-v3',
                            capture_binding=BINDING.to_dict(), previous=BOOTSTRAP.to_dict())
    window['end'] = BINDING.checkpoint(IDENTITY, 20).to_dict()
    document['window_destination_proof'] = value
    return document


def test_bound_window_projection_accepts_full_context_only():
    result = project_console_document(bound_document(), LIVE_SOURCE, NOW).to_dict()['window_delivery']
    assert result['state'] == 'matched'


@pytest.mark.parametrize('change', ('epoch', 'scope', 'projection', 'domain', 'legacy-version'))
def test_bound_window_projection_refuses_incompatible_context(change):
    document = deepcopy(bound_document())
    window = document['window_destination_proof']['window']
    if change in ('epoch', 'scope'):
        window['end']['capture_epoch' if change == 'epoch' else 'scope_id'] = 'other'
    elif change == 'projection':
        window['end'] = {'receiver': 'R1', 'sequence': 20}
    elif change == 'domain':
        window['intent']['capture_binding']['source_system'] = 'other'
    else:
        window['intent']['format_version'] = 'quadringent-window-intent-v2'
    assert project_console_document(document, LIVE_SOURCE, NOW).to_dict()['window_delivery']['state'] == 'invalid'
