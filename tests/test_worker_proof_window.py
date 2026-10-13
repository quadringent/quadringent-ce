import os
import json
import io
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest

import site_fixture
import as400_continuous_capture as capture
from capture_binding_fakes import installed_capture_state

SITE = site_fixture.build_test_site()


ENV={**site_fixture.TEST_SITE_ENV,
     'AS400_RAW_BUCKET':SITE.raw_bucket,
     'ISERIES_HOST':SITE.ibmi_host,'ISERIES_USER':SITE.ibmi_user,
     'AS400_RAW_PREFIX':f'{SITE.stream_prefix}/runs/r1',
     'AS400_CHECKPOINT_TABLE':SITE.checkpoint_table,
     'AS400_STREAM_KEY':SITE.stream_prefix,
     'ISERIES_SCHEMA':SITE.source_schema,'ISERIES_TABLE':SITE.proof_table,
     'AS400_JOURNAL_LIBRARY':SITE.source_schema,'AS400_JOURNAL_NAME':SITE.journal_name}


class WorkerProofWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.admission = installed_capture_state(temporary.name, ENV)
        self.environment = self.admission.environment
        self.store = Mock()
        self.store.prefix = ENV['AS400_RAW_PREFIX']
        self.store.list_receipt_keys.return_value = ()
        self.checkpoint_factory = Mock(return_value=self.admission.checkpoint)
        self.object_factory = Mock(side_effect=lambda backend, prefix:
                                   self.admission.authority_store if prefix == 'receiver-authority-v1'
                                   else self.store)
        for name, factory in (('_checkpoint_store', self.checkpoint_factory),
                              ('_object_store', self.object_factory)):
            patcher = patch.object(capture, name, factory)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cli_wires_chain_budget_and_publishes_truthful_terminal_state(self):
        for complete, expected_code in ((True, 0), (False, 2)):
            with self.subTest(complete=complete), patch.dict(os.environ, {**self.environment, 'AS400_JAVA_CLASSPATH': 'test-only'}, clear=True), patch('sys.argv', ['capture', '--proof-window-id', 'w1', '--proof-window-count', '3', '--max-seconds', '2281']), patch.object(capture, 'PersistentJavaWorker') as worker, patch.object(capture,'DynamoDbSourceGate'), patch('quadringent.proof_windows.prepare_window') as prepare, patch('quadringent.proof_windows.prepare_window_chain') as chain, patch.object(capture, 'ContinuousCaptureService') as services, patch.object(capture, '_console_sink', return_value=None), patch.object(capture, 'CachedReceiverCatalog') as catalog, patch.object(capture, 'ConsoleSnapshotBuilder') as console, redirect_stdout(io.StringIO()) as output:
                store = self.store
                def read(key, limit):
                    if key == 'window-chain.json':
                        raise FileNotFoundError(key)
                    return b'{"format_version":"quadringent-run-reservation-v1","run_id":"r1"}'
                store.get_bounded.side_effect = read
                catalog.return_value.snapshot.return_value = []
                service = services.return_value
                service.proof_window_count = 3
                service.proof_windows_complete = complete
                service.run.return_value = {}
                self.assertEqual(capture.main(), expected_code)
                self.assertEqual(services.call_args.kwargs['proof_window_count'], 3)
                self.assertEqual(services.call_args.kwargs['capture_binding'], self.admission.binding)
                prepare.assert_called_once()
                self.assertEqual(prepare.call_args.kwargs['capture_binding'], self.admission.binding)
                chain.assert_called_once_with(store, self.admission.checkpoint, initial_window_id='w1',
                                              window_count=3, capture_binding=self.admission.binding)
                expected_state = 'STOPPED_PROOF_CHAIN' if complete else 'STOPPED_BUDGET'
                self.assertEqual(console.return_value.mark_stopped.call_args.args[0], expected_state)
                finished = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(finished['proof_windows_complete'], complete)
                self.assertEqual(finished['state'], expected_state)
                worker.return_value.close.assert_called_once()

    def test_chain_requires_valid_count_and_sufficient_explicit_time_budget(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertIsNone(capture._proof_chain_options(None, None, 600, None))
            self.assertEqual(capture._proof_chain_options('w1', 3, 600, 2281), 3)
            for window_id, count, seconds in ((None, 3, 2281), ('w1', True, 2281), ('w1', 0, 2281), ('w1', 129, 999999), ('w1', 3, None), ('w1', 3, 2280)):
                with self.subTest(window_id=window_id, count=count, seconds=seconds):
                    with self.assertRaises(ValueError):
                        capture._proof_chain_options(window_id, count, 600, seconds)

    def test_existing_chain_mismatch_is_rejected_before_window_preparation(self):
        with patch.dict(os.environ, self.environment, clear=True), patch('sys.argv', ['capture', '--proof-window-id', 'other-root', '--proof-window-count', '3', '--max-seconds', '2281']), patch.object(capture, 'PersistentJavaWorker') as worker, patch('quadringent.proof_windows.prepare_window') as prepare, patch('quadringent.proof_windows.prepare_window_chain', side_effect=ValueError('window chain restart configuration differs')) as chain:
            self.store.get_bounded.return_value = b'{}'
            with self.assertRaisesRegex(ValueError, 'restart configuration differs'):
                capture.main()
            chain.assert_called_once()
            worker.assert_not_called()
            prepare.assert_not_called()

    def test_nonfinite_reader_budget_is_rejected(self):
        for timeout in ('nan', 'inf', '-inf'):
            with self.subTest(timeout=timeout), patch.dict(os.environ, {'AS400_READER_TIMEOUT_SECONDS': timeout}):
                with self.assertRaises(ValueError):
                    capture._proof_chain_options('w1', 3, 600, 999999)

    def test_closure_grace_is_reserved_for_every_successor(self):
        with patch.dict(os.environ, {'AS400_READER_TIMEOUT_SECONDS': '15'}):
            for insufficient in (1276, 1335):
                with self.assertRaises(ValueError):
                    capture._proof_chain_options('w1', 2, 600, insufficient)
            self.assertEqual(capture._proof_chain_options('w1', 2, 600, 1336), 2)

    def test_chain_terminal_state_does_not_claim_delivery(self):
        from types import SimpleNamespace
        for complete, state, code in ((True, 'STOPPED_PROOF_CHAIN', 0), (False, 'STOPPED_BUDGET', 2)):
            service = SimpleNamespace(proof_window_count=3, proof_windows_complete=complete)
            actual_state, reason, actual_code = capture._capture_stop(service, budget_reached=True)
            self.assertEqual((actual_state, actual_code), (state, code))
            self.assertIn('Snowflake', reason)

    def test_chain_read_failure_does_not_allow_source_access(self):
        for error in (PermissionError('denied'), TimeoutError('unavailable')):
            with self.subTest(error=type(error).__name__), patch.dict(os.environ, self.environment, clear=True), patch('sys.argv', ['capture']), patch.object(capture, 'PersistentJavaWorker') as worker:
                self.store.get_bounded.side_effect = error
                with self.assertRaises(type(error)):
                    capture.main()
                worker.assert_not_called()
                self.store.put_once.assert_not_called()
                self.assertEqual(self.admission.checkpoint.load_bound(), self.admission.binding.bootstrap)

    def test_absent_chain_preserves_admitted_startup(self):
        with patch.dict(os.environ, {**self.environment, 'AS400_JAVA_CLASSPATH': 'test-only'}, clear=True), patch('sys.argv', ['capture']), patch.object(capture, 'PersistentJavaWorker', side_effect=RuntimeError('source boundary reached')) as worker, patch.object(capture,'DynamoDbSourceGate'):
            self.store.get_bounded.side_effect = FileNotFoundError('window-chain.json')
            with self.assertRaisesRegex(RuntimeError, 'source boundary reached'):
                capture.main()
            worker.assert_called_once()
            self.store.put_once.assert_not_called()

    def test_durable_chain_cannot_be_disabled_before_source_or_window_mutation(self):
        for arguments in ([], ['--proof-window-id', 'w1']):
            with self.subTest(arguments=arguments), patch.dict(os.environ, self.environment, clear=True), patch('sys.argv', ['capture', *arguments]), patch.object(capture, 'PersistentJavaWorker') as worker, patch('quadringent.proof_windows.prepare_window') as prepare:
                store = self.store
                store.get_bounded.return_value = b'{"format_version":"quadringent-window-chain-v1"}'
                with self.assertRaisesRegex(ValueError, 'cannot omit the durable window budget'):
                    capture.main()
                self.assertEqual(store.get_bounded.call_args.args[0], 'window-chain.json')
                worker.assert_not_called()
                prepare.assert_not_called()
                store.put_once.assert_not_called()

    def test_run_marker_checked_before_window_or_source(self):
        for mode in ('missing','wrong','valid'):
            with self.subTest(mode=mode),patch.dict(os.environ,self.environment,clear=True),patch('sys.argv',['capture','--proof-window-id','w1']),patch.object(capture,'PersistentJavaWorker') as worker,patch('quadringent.proof_windows.prepare_window',side_effect=ValueError('stop before source')) as prepare:
                store=self.store
                def read(key, limit):
                    if key == 'window-chain.json' or mode == 'missing':
                        raise FileNotFoundError(key)
                    return json.dumps({'format_version':'quadringent-run-reservation-v1','run_id':'r1' if mode=='valid' else 'other'}).encode()
                store.get_bounded.side_effect = read
                if mode == 'missing':
                    with self.assertRaises(FileNotFoundError):
                        capture.main()
                else:
                    message = 'stop before source' if mode == 'valid' else 'missing or incompatible isolated run reservation'
                    with self.assertRaisesRegex(ValueError, message):
                        capture.main()
                worker.assert_not_called()
                if mode=='valid':prepare.assert_called_once()
                else:prepare.assert_not_called()

    def test_disabled_by_default_and_exact_dev_configuration(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertIsNone(capture._proof_window_options(None,600))
        with patch.dict(os.environ,ENV,clear=True):
            self.assertEqual(capture._proof_window_options('w1',600),{
                'window_id':'w1','duration_seconds':600,'stream_id':SITE.stream_prefix})

    def test_invalid_scope_or_bootstrap_rejected_before_source_construction(self):
        for mutation in ({'ISERIES_HOST':'other'}, {'ISERIES_USER':'other'}, {'AS400_RAW_BUCKET':'prod-bucket'}, {'AS400_RAW_PREFIX':'other/foreign/lane'},
                         {'ISERIES_SCHEMA':'OTHERLIB'}, {'ISERIES_TABLES':'SALE,CNTR'},
                         {'AS400_BOOTSTRAP_RECEIVER':'__TAIL__'}, {'AS400_CHECKPOINT_TABLE':'other'}):
            with self.subTest(mutation=mutation),patch.dict(os.environ,{**self.environment,**mutation},clear=True),patch('sys.argv',['capture','--proof-window-id','w1']),patch.object(capture,'PersistentJavaWorker') as worker:
                with self.assertRaises(ValueError):capture.main()
                worker.assert_not_called()
                self.checkpoint_factory.assert_not_called()
                self.object_factory.assert_not_called()
