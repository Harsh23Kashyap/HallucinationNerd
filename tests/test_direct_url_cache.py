"""Direct-URL references must not reuse another source's cached text. Offline."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import citation_resolver as cr

class DirectURLCacheTests(unittest.TestCase):
    def setUp(self):self.saved=cr._source_cache;cr._source_cache={}
    def tearDown(self):cr._source_cache=self.saved
    def key(self,**info):return cr._make_cache_key(info,'1')
    def test_different_urls_with_same_title_do_not_share(self):
        self.assertNotEqual(self.key(title='Annual report',url='https://a.invalid/report'),self.key(title='Annual report',url='https://b.invalid/report'))
    def test_url_only_references_at_same_number_do_not_share(self):
        self.assertNotEqual(self.key(url='https://a.invalid/report'),self.key(url='https://b.invalid/report'))
    def test_case_sensitive_paths_stay_distinct(self):
        self.assertNotEqual(self.key(url='https://a.invalid/Report'),self.key(url='https://a.invalid/report'))
    def test_query_selected_documents_stay_distinct(self):
        self.assertNotEqual(self.key(url='https://a.invalid/report?id=1'),self.key(url='https://a.invalid/report?id=2'))
    def test_same_url_with_different_display_titles_shares(self):
        self.assertEqual(self.key(title='Old display title',url='https://a.invalid/report'),self.key(title='New display title',url='https://a.invalid/report'))
    def test_reference_number_is_not_url_identity(self):
        info={'url':'https://a.invalid/report'}
        self.assertEqual(cr._make_cache_key(info,'1'),cr._make_cache_key(info,'17'))
    def test_surrounding_url_whitespace_does_not_change_identity(self):
        self.assertEqual(self.key(url=' https://a.invalid/report '),self.key(url='https://a.invalid/report'))
    def test_arxiv_still_has_precedence(self):
        self.assertEqual(self.key(arxiv_id='2305.14314',url='https://a.invalid'),self.key(arxiv_id='2305.14314',url='https://b.invalid'))
    def test_doi_still_has_precedence(self):
        self.assertEqual(self.key(doi='10.1234/ABC',url='https://a.invalid'),self.key(doi='10.1234/abc',url='https://b.invalid'))
    def test_direct_url_precedes_pmid_like_fetch_path(self):
        self.assertNotEqual(self.key(pmid='12345',url='https://a.invalid/report'),self.key(pmid='12345',url='https://b.invalid/report'))
    def test_no_url_preserves_pmid_identity(self):
        self.assertEqual(self.key(pmid='12345',title='a'),self.key(pmid='12345',title='b'))
    def test_no_identifier_preserves_title_fallback(self):
        self.assertEqual(self.key(title='Annual report'),self.key(title='ANNUAL REPORT'))
    def test_actual_resolver_does_not_reuse_another_document_url(self):
        infos=[{'1':{'title':'Annual report','url':'https://a.invalid/report'}},{'1':{'title':'Annual report','url':'https://b.invalid/report'}}]
        with patch.object(cr,'resolve_references',side_effect=infos),patch.object(cr,'_fetch_with_policy',side_effect=lambda i,patient:(i['url'],False)) as fetch:
            self.assertEqual(cr.resolve_and_fetch_all('A',['1'])['1'],'https://a.invalid/report')
            self.assertEqual(cr.resolve_and_fetch_all('B',['1'])['1'],'https://b.invalid/report')
            self.assertEqual(fetch.call_count,2)
    def test_actual_resolver_reuses_same_url_under_new_number(self):
        infos=[{'1':{'title':'Old name','url':'https://a.invalid/report'}},{'17':{'title':'New name','url':'https://a.invalid/report'}}]
        with patch.object(cr,'resolve_references',side_effect=infos),patch.object(cr,'_fetch_with_policy',return_value=('cached source',False)) as fetch:
            cr.resolve_and_fetch_all('A',['1'])
            self.assertEqual(cr.resolve_and_fetch_all('B',['17'])['17'],'cached source')
            self.assertEqual(fetch.call_count,1)

if __name__=='__main__':unittest.main()
