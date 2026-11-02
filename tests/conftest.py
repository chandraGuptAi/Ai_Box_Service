import os

# Tests must not depend on the developer's .env (environment variables take precedence over it).
os.environ["SERVICE_TOKEN"] = ""
os.environ['DEBUG'] = 'false'
os.environ['ATTACHMENT_VISION_BASE_URL'] = ''
os.environ['ATTACHMENT_VISION_MODEL'] = ''
os.environ['ATTACHMENT_VISION_API_KEY'] = ''
# Report memory: spelling similarity only and a throwaway database, unless a test opts in.
os.environ['REPORT_MEMORY_SEMANTIC'] = 'false'
import tempfile
os.environ['REPORT_MEMORY_DB'] = os.path.join(tempfile.mkdtemp(), 'report_memory.sqlite')
