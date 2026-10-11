"""Token model contracts, binary identity and candidate-domain failure gates."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare_dense_quality import TOKEN_CONTRACT, entries
from quality_tokens import checked_matrix, check_score_order, durable, kept_positions, load_tokens, prefixed_ids
from quality_vectors import validate_measurement


class TokenQuality(unittest.TestCase):
    def test_locked_token_assets_fit_existing_shared_budget(self):
        contract = json.loads(TOKEN_CONTRACT.read_bytes())
        rows = entries(contract)
        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(r['bytes'] for _, r in rows), 71649484)
        self.assertLess(sum(r['bytes'] for _, r in rows)+525600226, 1024**3)
        self.assertTrue(all(name.startswith('colbert17m/') for name, _ in rows))
        self.assertLessEqual(48*128*48*64, 20000000)

    def test_token_contract_refuses_wrong_model_width_pooling_scope_and_revision(self):
        for key, value in [('model_id', 'other/model'), ('revision', 'main'), ('dimensions', 96),
                           ('query_tokens', 128), ('max_tokens', 512), ('pooling', 'mean'),
                           ('datasets', ['t2retrieval']), ('normalization', 'none')]:
            contract = json.loads(TOKEN_CONTRACT.read_bytes()); contract[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): entries(contract)

    def test_prefix_insertion_and_right_truncation_keep_special_tokens(self):
        self.assertEqual(prefixed_ids([101, 10, 11, 102], 1, 8), [101, 1, 10, 11, 102])
        self.assertEqual(prefixed_ids([101, 10, 11, 12, 13, 102], 2, 5), [101, 2, 10, 11, 102])
        for ids, maximum in [([], 48), ([101], 48), ([101, 102], 2)]:
            with self.assertRaises(ValueError): prefixed_ids(ids, 1, maximum)

    def test_document_punctuation_padding_and_query_tokens_are_distinct(self):
        ids = [101, 2, 10, 99, 102, 103]
        attention = [1, 1, 1, 1, 1, 0]
        self.assertEqual(kept_positions(ids, attention, {99}, False), [0, 1, 2, 4])
        self.assertEqual(kept_positions(ids, attention, {99}, True), [0, 1, 2, 3, 4])
        for mask in [[0]*6, [1, 1, 1, 1, 1, 2], [True]*6, [1]*5]:
            with self.assertRaises(ValueError): kept_positions(ids, mask, {99}, False)

    def test_matrix_refuses_empty_ragged_nonfinite_and_nonunit_vectors(self):
        unit = [[1.] + [0.]*47]
        self.assertIs(checked_matrix(unit, 128), unit)
        for value in [[], [[1.]], unit*129, [[True]+[0.]*47], [[float('nan')]+[0.]*47], [[2.]+[0.]*47]]:
            with self.assertRaises(ValueError): checked_matrix(value, 128)

    def test_score_oracle_refuses_outsiders_missing_winners_duplicates_and_wrong_scores(self):
        candidates = [str(i) for i in range(12)]
        scores = {key: float(key) for key in candidates}
        hits = [{'key': str(11-i), 'rank': i+1, 'score': float(11-i)} for i in range(10)]
        self.assertEqual(check_score_order(candidates, hits, scores)['maximum_score_error'], 0)
        for field, value in [('key', 'outside'), ('key', '0'), ('score', 999), ('score', float('nan')), ('rank', 2)]:
            bad = copy.deepcopy(hits); bad[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): check_score_order(candidates, bad, scores)
        bad = copy.deepcopy(hits); bad[0] = dict(bad[1], rank=1)
        with self.assertRaises(ValueError): check_score_order(candidates, bad, scores)
        with self.assertRaises(ValueError): check_score_order(candidates, hits[:-1], scores)

    def test_equal_score_order_is_not_falsely_promised(self):
        hits = [{'key': 'b', 'rank': 1, 'score': 1}, {'key': 'a', 'rank': 2, 'score': 1}]
        self.assertEqual(check_score_order(['a', 'b'], hits, {'a': 1, 'b': 1})['maximum_score_error'], 0)

    def test_genuine_zero_candidates_are_reported_without_fabricated_hits(self):
        self.assertEqual(check_score_order([], [], {})['maximum_score_error'], 0)
        for candidates, hits, scores in [([], [{'key': 'a'}], {}), (['a'], [], {'a': 1}), ([], [], {'a': 1})]:
            with self.assertRaises(ValueError): check_score_order(candidates, hits, scores)

    def example(self):
        fixture = {'documents': [{'id': 'd', 'body': 'exact body'}], 'queries': [{'id': 'q', 'text': 'query'}]}
        data = struct.pack('<96f', *([1.]+[0.]*47)*2)
        index = {'fixture_sha256': 'a'*64}
        for i, (kind, field) in enumerate([('documents', 'body'), ('queries', 'text')]):
            row = fixture[kind][0]
            index[kind] = {row['id']: dict(text_sha256=hashlib.sha256(row[field].encode()).hexdigest(),
                offset=i*48*4, tokens=1, encoded_tokens=3, untruncated_tokens=3)}
        index['queries']['q'].update(tokens=3, offset=48*4)
        data += struct.pack('<96f', *([1.]+[0.]*47)*2)
        return fixture, index, data

    def publish(self, root, index, data, **changes):
        raw = json.dumps(index).encode()
        manifest = dict(schema_version=1, status='encoded', release_supported=False, errors=[],
            model_contract_sha256=hashlib.sha256(b'contract').hexdigest(),fixture_sha256='a'*64,
            outputs={name: dict(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                     for name, body in [('scifact.json',raw),('scifact.f32',data)]})
        manifest.update(changes)
        (root/'manifest.json').write_text(json.dumps(manifest))
        (root/'scifact.json').write_bytes(raw); (root/'scifact.f32').write_bytes(data)

    def test_binary_receipt_checks_exact_text_shape_offsets_and_no_trailing_bytes(self):
        fixture, index, data = self.example()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.publish(root,index,data)
            self.assertEqual(load_tokens(root, fixture, 'a'*64, b'contract')[0]['queries']['q']['vector'][0][0], 1)
            for field, value in [('offset', 4), ('offset', False), ('tokens', 129), ('text_sha256', 'b'*64),
                                 ('encoded_tokens', True), ('untruncated_tokens', 999)]:
                bad = copy.deepcopy(index); bad['documents']['d'][field] = value
                self.publish(root,bad,data)
                with self.subTest(field=field), self.assertRaises(ValueError): load_tokens(root, fixture, 'a'*64, b'contract')
            for altered in [data[:-4], data+b'extra', b'\0'*len(data)]:
                self.publish(root,index,altered)
                with self.assertRaises(ValueError): load_tokens(root, fixture, 'a'*64, b'contract')

    def test_partial_or_tampered_receipt_never_becomes_ready(self):
        fixture,index,data = self.example()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key,value in [('status','failed'), ('errors',['incomplete']), ('release_supported',True),
                              ('model_contract_sha256','x'*64), ('fixture_sha256','b'*64)]:
                self.publish(root,index,data,**{key:value})
                with self.assertRaises(ValueError): load_tokens(root,fixture,'a'*64,b'contract')
            self.publish(root,index,data); (root/'scifact.f32').write_bytes(data[:-1]+b'!')
            with self.assertRaises(ValueError): load_tokens(root,fixture,'a'*64,b'contract')

    def test_ticket_success_requires_all_durable_fields(self):
        state = dict(committed=True,applied=True,durable=True,pending_events=0,timed_out=False,error=None,generation=1)
        def callback(current): return Mock(side_effect=['00000000-0000-0000-0000-000000000001',json.dumps(current)])
        self.assertEqual(durable(callback(state),'UPDATE fixture','fixture',lambda s:"'"+s+"'"),state)
        for key,value in [('committed',False),('applied',False),('durable',False),('pending_events',1),('timed_out',True),('error','failure')]:
            bad=dict(state,**{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError): durable(callback(bad),'UPDATE fixture','fixture',lambda s:"'"+s+"'")


if __name__ == '__main__':
    unittest.main()
