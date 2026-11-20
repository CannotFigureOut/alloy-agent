from fastapi.testclient import TestClient
from app import config
from app.main import app
from app.model import CompatibleBackend


def test_save_reload_and_secret_redaction(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(config,'model_settings',config.ModelSettings())
    (tmp_path/'.env').write_text('DATABASE_PATH=data/test.db\n')
    with TestClient(app) as client:
        body={'base_url':'https://example.com/v1/','api_key':'test-secret','model_name':'test-model'}
        response=client.post('/api/model-settings',json=body)
        assert response.status_code==200
        assert 'test-secret' not in response.text
        assert config.active_mode()=='openai'
        assert client.get('/api/config').json()['model']=='test-model'
        assert 'DATABASE_PATH=data/test.db' in (tmp_path/'.env').read_text()
        assert 'MODEL_API_KEY' in (tmp_path/'.env').read_text()
        body['api_key']=''
        body['model_name']='another-model'
        assert client.post('/api/model-settings',json=body).status_code==200
        assert config.model_settings.api_key=='test-secret'
        body['base_url']='file:///tmp/test'
        assert client.post('/api/model-settings',json=body).status_code==422


def test_connection_uses_draft_and_does_not_save(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(config,'model_settings',config.ModelSettings())
    async def complete(self,messages,max_tokens):
        assert self.settings.model_name=='draft'
        assert self.settings.api_key=='draft-secret'
        return 'OK'
    monkeypatch.setattr(CompatibleBackend,'complete',complete)
    body={'base_url':'https://example.com/v1','api_key':'draft-secret','model_name':'draft'}
    with TestClient(app) as client:
        assert client.post('/api/model-settings/test',json=body).json()['ok']
        assert not (tmp_path/'.env').exists()
        assert config.model_settings.model_name==''
        async def failed(*args,**kwargs):raise RuntimeError('draft-secret')
        monkeypatch.setattr(CompatibleBackend,'complete',failed)
        response=client.post('/api/model-settings/test',json=body)
        assert response.status_code==502
        assert 'draft-secret' not in response.text
