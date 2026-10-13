import os
import sys

# Add repository root to Python path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from app import app


class VercelPathMiddleware:
    """WSGI Middleware for Vercel:
    Restores the original PATH_INFO from x-forwarded-uri or x-matched-path headers
    if Vercel rewrote the request URL to /api/index.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        forwarded_uri = environ.get('HTTP_X_FORWARDED_URI') or environ.get('HTTP_X_MATCHED_PATH')
        if forwarded_uri and forwarded_uri not in ('/api/index', '/api/index.py'):
            path = forwarded_uri.split('?')[0]
            environ['PATH_INFO'] = path
            environ['REQUEST_URI'] = forwarded_uri
        return self.wsgi_app(environ, start_response)


app.wsgi_app = VercelPathMiddleware(app.wsgi_app)
