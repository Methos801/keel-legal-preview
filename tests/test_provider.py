import asyncio, json, pathlib, sys, unittest
import httpx

from keel.legal.controls import extract_chronology, lint_legal_output
from keel.providers.base import ModelSpec
from keel.providers.openai_compat import OpenAICompatProvider

class KeelSmoke(unittest.IsolatedAsyncioTestCase):
    def test_evidence_and_status_retained(self):
        out=extract_chronology([{'sid':'S0002','locator':'p. 1','text':'Delivered on 2026-05-04.'},{'sid':'S0001','locator':'p. 2','text':'Signed on 2026-05-03.'}])
        self.assertEqual([(x['date'],x['sid']) for x in out],[('2026-05-03','S0001'),('2026-05-04','S0002')])
        self.assertFalse(lint_legal_output('Unlabeled summary',lambda sid: False)['ok'])
        self.assertFalse(lint_legal_output('DRAFT\nReference [S9999 p. 2].',lambda sid: sid=='S0001')['ok'])

    async def test_provider_posts_only_to_mock(self):
        seen=[]
        def response(req):
            self.assertEqual((req.method,req.url.host,req.url.path),('POST','fixture.invalid','/v1/chat/completions'))
            body=json.loads(req.content); seen.append(body)
            return httpx.Response(200,json={'choices':[{'message':{'content':'synthetic response'},'finish_reason':'stop'}],'usage':{'prompt_tokens':4,'completion_tokens':2,'total_tokens':6}})
        async with httpx.AsyncClient(base_url='https://fixture.invalid/v1/',transport=httpx.MockTransport(response),trust_env=False) as c:
            result=await OpenAICompatProvider(ModelSpec(model_id='fixture',provider='fixture',base_url='https://fixture.invalid/v1'),client=c).chat([{'role':'user','content':'synthetic prompt'}])
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['messages'][0]['content'],'synthetic prompt')
        self.assertEqual(result.content,'synthetic response')

if __name__=='__main__': unittest.main(verbosity=2)
