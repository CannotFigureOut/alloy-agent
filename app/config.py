import os
from pathlib import Path
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
MODE = os.getenv('AGENT_MODE', 'demo')
MODEL = os.getenv('MODEL', 'gemini-2.5-flash')
DB = Path(os.getenv('DATABASE_PATH', str(ROOT / 'data/alloy.db')))
TIMEOUT = float(os.getenv('MODEL_TIMEOUT_SECONDS', '90'))

from dataclasses import dataclass
from dotenv import dotenv_values, set_key
import tempfile

@dataclass(frozen=True)
class ModelSettings:
    base_url: str = ''
    api_key: str = ''
    model_name: str = ''

_values = dotenv_values(ROOT / '.env')
model_settings = ModelSettings(
    _values.get('MODEL_BASE_URL') or os.getenv('MODEL_BASE_URL', ''),
    _values.get('MODEL_API_KEY') or os.getenv('MODEL_API_KEY', ''),
    _values.get('MODEL_NAME') or os.getenv('MODEL_NAME', ''),
)

def save_model_settings(settings):
    """先原子替换配置文件，再发布不可变的运行时配置快照。"""
    global model_settings
    path = ROOT / '.env'
    fd, name = tempfile.mkstemp(prefix='.env-', dir=ROOT)
    os.close(fd)
    temp = Path(name)
    try:
        temp.write_text(path.read_text() if path.exists() else '', encoding='utf-8')
        for key, value in {'MODEL_BASE_URL': settings.base_url, 'MODEL_API_KEY': settings.api_key,
                           'MODEL_NAME': settings.model_name, 'AGENT_MODE': 'openai'}.items():
            set_key(str(temp), key, value)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    model_settings = settings

def active_mode():
    return 'openai' if model_settings.base_url and model_settings.model_name else MODE
