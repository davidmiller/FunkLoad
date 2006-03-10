# (C) Copyright 2006 Nuxeo SAS <http://nuxeo.com>
# Author: bdelbosc@nuxeo.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA
# 02111-1307, USA.
#
"""Browser implementation.

$Id$
"""
import sys
import time
import logging
from urlparse import urljoin
from optparse import OptionParser, TitledHelpFormatter
from curlfetcher import CurlFetcher
from webunitfetcher import WebunitFetcher
from htmlresourceparser import HTMLResourceParser
from utils import get_logger, get_version, truncate

class Browser:
    """Simulate a browser using a fetcher.

    Handles redirects, referer, fetching html resources, history.
    Simulates a cache for resources."""

    def __init__(self, fetcher_cls):
        logger = get_logger('funkload.browser')
        self.logd = logger.debug
        self.logi = logger.info
        self.logw = logger.warning
        self.logger = logger
        self.fetcher = fetcher_cls()
        self.fetch = self.fetcher.fetch

        self.setHeader = self.fetcher.setHeader
        self.setUserAgent = self.fetcher.setUserAgent
        self.setBasicAuth = self.fetcher.setBasicAuth
        self.clearBasicAuth = self.fetcher.clearBasicAuth

        self.page_history = []
        self.request_history = []
        self.auto_referer = True        # set referer automaticly
        self.max_redirs = 10            # number of redirects to follow
        self.fetch_resources = True     # extract html resources
        self.use_resource_cache = True  # simulate a cache for resources
        self.setUserAgent('FunkLoad/%s' % get_version())

    def browse(self, url_in, params_in, method=None, fetch_resources=None,
               use_resource_cache=None):
        """Handle redirect and fetch HTML ressources.

        return a list of HTTPResponses"""
        responses = []
        request_history = self.request_history
        if fetch_resources is None:
            fetch_resources = self.fetch_resources
        if use_resource_cache is None:
            use_resource_cache = self.use_resource_cache
        if method is None:
            method = params_in and 'post' or 'get'

        # 1. fetch the requested page
        self.logd('%s: %s' % (method, url_in | truncate(70)))
        response = self.fetch(url_in, params_in, method)
        responses.append(response)
        request_history.append((method, url_in, params_in))
        self.setReferer(url_in, False)
        self.logd(' return code %s done in %.6fs.' % (
            response.code, response.total_time))

        # 2. handles redirection
        redirect_count = self.max_redirs
        while response.code in (301, 302):
            url = response.getHeader('Location')
            url = urljoin(url_in, url)
            self.logd(' redirect: %s' % url | truncate(70))
            response = self.fetch(url, params_in, method)
            self.logd('  return code %s done in %.6fs.' % (
                response.code, response.total_time))
            responses.append(response)
            request_history.append((method, url, params_in))
            self.setReferer(url, False)
            redirect_count -= 1
            if not redirect_count:
                self.logw('Too many redirects (%s) give up after: %s.' % (
                    self.max_redirs, url))
                break

        # 3. extract html resources
        if (fetch_resources and
            response.content_type and response.content_type.count('html')):
            parser = HTMLResourceParser(response.effective_url)
            parser.feed(response.body)
            parser.close()
            links = parser.links

            if use_resource_cache:
                # 4. simulate an optimal cache
                links = [link for link in links
                         if ('get', link, None) not in self.request_history]
            for link in links:
                self.logd(' fetch resource:  %s' % link | truncate(70))
                response = self.fetch(link, method='get')
                responses.append(response)
                request_history.append((method, link, params_in))
                self.logd('  return code %s done in %.6fs.' % (
                    response.code, response.total_time))

        return responses

    def post(self, url_in, params_in=None):
        """Simulate a browser post."""
        responses = self.browse(url_in, params_in, method='post')
        self.page_history.append(('post', url_in, params_in))
        return responses

    def get(self, url_in, params_in=None):
        """Simulate a browser get."""
        responses = self.browse(url_in, params_in=None, method='get')
        self.page_history.append(('get', url_in, params_in))
        return responses

    def setReferer(self, url, force=True):
        """Set the referer."""
        if force or self.auto_referer:
            self.fetcher.setReferer(url)

    def perf(self, url, params=None, method=None, count=10):
        """Loop on a request output stats."""
        stats = {}
        start = time.time()
        volume = 0
        requests = 0
        url_order = []
        for i in xrange(count):
            responses = self.browse(url, params, method)
            for response in responses:
                url = response.url
                if url not in url_order:
                    url_order.append(url)
                stats.setdefault(url, []).append(
                    (response.total_time,
                     response.connect_time,
                     response.transfer_time))
                volume += response.size_download
                requests += 1
        stop = time.time()
        self._renderStat(stats, requests, stop-start, volume, url_order)

    def _computeStat(self, times):
        """Returns the (average, standard deviation,
            min, median, percentil 90, 95, 98, max,
            per second) of a list of times."""
        total = sum(times)
        avg = total / len(times)
        pers = avg and 1/avg or 0
        count = len(times)
        stddev = (sum([(i - avg)**2 for i in times]) / (count - 1 or 1)) ** .5
        sort = list(times)
        sort.sort()
        return (avg, stddev,
                sort[0], sort[count//2],
                sort[int(count * .90)], sort[int(count * .95)],
                sort[int(count * .98)], sort[-1], pers)

    def _renderStat(self, stats, requests, elapsed, volume, url_order):
        """Render perf stats."""
        self.logi("Performing %d requests, during %.3fs, download: %.2fKb" % (
            requests, elapsed, volume/1024))
        self.logi("  Effective requests per second: %.3f RPS" % (
            requests/elapsed))
        self.logi("                 Transfert rate: %.3f Kb/s\n" % (
            volume/elapsed/1024))
        thead = ('average:', 'std dev:', 'minimum:', 'median:',
                 '90%:', '95%:', '98%:', 'maximum:', 'per second:')
        for request in url_order:
            values = stats[request]
            self.logi("Stat for %d requests of: %s" % (len(values), request))
            self.logi("                 total      connect    transfert")
            self.logi("----------- ----------- ------------ ------------")
            times = zip(*[self._computeStat(x) for x in zip(*values)])
            for title, line in zip(thead, times):
                self.logi("%11s" % title + "%12.6f %12.6f %12.6f" % line)
            self.logi("----------- ----------- ------------ ------------\n")


class BrowserProgram:
    """Simple browser command line."""

    USAGE = """%prog [options] url [url2] ...

Simulate a browser request on urls.

See http://funkload.nuxeo.org/ for more information.


Examples
========
  %prog http://localhost/ -d
                        Display requests used to browse http://localhost/
  %prog http://localhost/ -d -S
                        Do not fetch html resources.
  %prog http://localhost/ -d --webunit
                        Use the WebUnit fetcher (default is pyCurl).
  %prog http://localhost/ -D
                        Dump responses.
  %prog http://localhost/ -t
                        Verbose trace for request.
  %prog http://localhost/ -u login:pwd -d
                        Use http basic auth.
  %prog http://localhost/ -n 100
                        Perform 100 requests and output detail statistics.
"""

    def __init__(self, argv=None):
        if argv is None:
            argv = sys.argv
        options, args = self.parseArgs(argv)
        if options.trace or options.debug:
            logger = get_logger('funkload.browser', level=logging.DEBUG)
        else:
            logger = get_logger('funkload.browser', level=logging.INFO)
        self.logi = logger.info
        if options.webunit:
            # webunit fetcher
            browser = Browser(WebunitFetcher)
        else:
            # curl fetcher setup
            browser = Browser(CurlFetcher)
            if options.trace:
                browser.fetcher.curlVerbose(1)
                options.debug = True
        self.options = options
        browser.fetch_resources = not options.simple_fetch
        if options.user_agent:
            browser.setUserAgent(options.user_agent)
        if options.user_password:
            cred = options.user_password.split(':', 1)
            browser.setBasicAuth(cred[0], cred[-1])
        if options.no_auto_referer:
            browser.auto_referer = False
        if options.no_cache:
            browser.use_resource_cache = False
        self.browser = browser
        self.urls = args[1:]

    def run(self, urls=None):
        """Browse urls."""
        browser = self.browser
        options = self.options
        use_http_post = options.post
        if urls is None:
            urls = self.urls

        for url in urls:
            if options.perf:
                browser.perf(url, count=int(options.perf))
            else:
                if use_http_post:
                    responses = browser.post(url)
                else:
                    responses = browser.get(url)
            if options.dump_responses:
                self.dumpResponses(responses)

    def parseArgs(self, argv):
        """Parse programs args."""
        parser = OptionParser(self.USAGE, formatter=TitledHelpFormatter(),
                              version="FunkLoad %s" % get_version())
        parser.add_option("-d", "--debug", action="store_true",
                          help="Debug mode.")
        parser.add_option("-t", "--trace", action="store_true",
                          help="Trace fetcher activity.")
        parser.add_option("-S", "--simple-fetch", action="store_true",
                          help="Don't load additional resources like css "
                          "or images when fetching an html page.")
        parser.add_option("", "--curl", action="store_true",
                          help="Use curl fetcher, "
                          "this is the default fetcher.")
        parser.add_option("", "--webunit", action="store_true",
                          help="Use Webunit fetcher.")
        parser.add_option("-G", "--get", action="store_true",
                          help="Send data with a HTTP GET.")
        parser.add_option("-P", "--post", action="store_true",
                          help="Send data with a HTTP POST.")
        parser.add_option("-D", "--dump-responses", action="store_true",
                          help="Dump responses.")
        parser.add_option("", "--no-auto-referer", action="store_true",
                          help="Don't set auto referer.")
        parser.add_option("-A", "--user-agent", type="string",
                          dest="user_agent",
                          help="User-Agent to send to server.")
        parser.add_option("", "--no-cache", action="store_true",
                          help="Don't cache resources already fetched.")
        parser.add_option("-u", "--user", type="string",
                          dest="user_password",
                          help="<user[:password]> "
                          "Set server basic auth user and password.")
        parser.add_option("-n", "--perf", type="int",
                          help="Number of requests to perform, return stats.")
        options, args = parser.parse_args(argv)
        if len(args) == 0:
            parser.error("incorrect number of arguments")

        return options, args

    def dumpResponses(self, responses):
        """Dump responses."""
        self.logi('Dump responses:')
        for response in responses:
            self.logi(response)

    def dumpHistory(self):
        """Dump history."""
        self.logi('Page history:')
        for page in self.browser.page_history:
            self.logi(page)

    def dumpRequestHistory(self):
        """Dump request history."""
        self.logi('Request history:')
        for request in self.browser.request_history:
            self.logi(request)


def main():
    """Default main."""
    prog = BrowserProgram()
    prog.run()


if __name__ == '__main__':
    main()
