import json

import bm

from ddtrace.propagation import _utils as utils
from ddtrace.propagation import http


class HTTPPropagationExtract(bm.Scenario):
    headers = bm.var(type=str)
    extra_headers = bm.var(type=int)
    wsgi_style = bm.var(type=bool)

    def generate_headers(self):
        headers = json.loads(self.headers)
        if self.wsgi_style:
            headers = {utils.get_wsgi_header(header): value for header, value in headers.items()}

        for i in range(self.extra_headers):
            header = "x-test-header-{}".format(i)
            if self.wsgi_style:
                header = utils.get_wsgi_header(header)
            headers[header] = str(i)

        return headers

    def run(self):
        headers = self.generate_headers()

        propagator = http.HTTPPropagator
        if self.wsgi_style:
            propagator = getattr(http, "WSGIPropagator", propagator)
        else:
            propagator = getattr(http, "LowercasePropagator", propagator)

        def _(loops):
            for _ in range(loops):
                propagator.extract(headers)

        yield _
