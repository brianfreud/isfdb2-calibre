#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '''(c) 2017 Adrianna Pińska <adrianna.pinska@gmail.com>,
Xtina Schelin <xtina.schelin@gmail.com>,
Grant Drake <grant.drake@gmail.com>'''
__docformat__ = 'restructuredtext en'

import time

from Queue import Queue, Empty
from threading import Thread

from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.sources.base import Source, Option
from calibre.ebooks.metadata.book.base import Metadata

#from calibre.utils.localization import get_udc

from calibre_plugins.isfdb.objects import Publication, PublicationsList, TitleList, TitleCovers


class ISFDB(Source):
    name = 'ISFDB'
    description = _('Downloads metadata and covers from ISFDB')
    author = 'Adrianna Pińska'
    version = (2, 0, 0)
    minimum_calibre_version = (0, 9, 33)

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset(['title', 'authors', 'identifier:isfdb', 'identifier:isfdb-catalog', 'identifier:isfdb-title', 'identifier:isbn', 'publisher', 'pubdate', 'comments'])

    options = (
        Option(
            'max_results',
            'number',
            5,
            _('Maximum number of search results to download:'),
            _('This setting only applies to ISBN and title / author searches. Book records with a valid ISFDB ID will return exactly one result.'),
        ),
    )

    has_html_comments = True
    supports_gzip_transfer_encoding = False
    cached_cover_url_is_reliable = True

    # TODO: delegate to helper objects
    #SEARCH_URL = 'http://www.isfdb.org/cgi-bin/se.cgi?%s'
    #ADV_SEARCH_URL = 'http://www.isfdb.org/cgi-bin/adv_search_results.cgi?%s'
    #ID_URL = 'http://www.isfdb.org/cgi-bin/pl.cgi?%s'

    def get_book_url(self, identifiers):
        isfdb_id = identifiers.get('isfdb', None)
        if isfdb_id:
            #url = self.ID_URL % isfdb_id
            url = Publication.url_from_id(isfdb_id)
            return ('isfdb', isfdb_id, url)

    def get_cached_cover_url(self, identifiers):
        isfdb_id = identifiers.get('isfdb', None)
        if isfdb_id:
            return self.cached_identifier_to_cover_url(isfdb_id)

        # If we have multiple books with the same ISBN and no ID this may reuse the same cover for multiple books
        # But we probably won't get into this situation, so let's leave this for now
        isbn = identifiers.get('isbn', None)
        if isbn:
            return self.cached_identifier_to_cover_url(self.cached_isbn_to_identifier(isbn))
                
        return None

    def _max_results(self):
        # At least one result, and no more than 10
        return max(min(self.prefs["max_results"], 10), 1)

    def identify(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30):
        '''
        This method will find exactly one result if an ISFDB ID is
        present, otherwise up to the maximum searching first for the
        ISBN and then for title and author.
        '''
        matches = set()
        relevance = {}

        # If we have an ISFDB ID, we use it to construct the publication URL directly
        
        isfdb_id = identifiers.get('isfdb', None)
        if isfdb_id:
            _, _, url = self.get_book_url(identifiers)
            matches.add(url)
            relevance[url] = 0 # most relevant
        else:
            if abort.is_set():
                return
            
            def add_matches(urls, relevance_score):
                for url in urls:
                    matches.add(url)
                    relevance[url] = relevance_score
                    
                    if len(matches) >= self._max_results():
                        break
            
            isbn = check_isbn(identifiers.get('isbn', None))
            catalog_id = identifiers.get('isfdb-catalog', None)

            # If there's an ISBN, search by ISBN first
            # Fall back to non-ISBN catalog ID -- ISFDB uses the same field for both.
            if isbn or catalog_id:
                query = PublicationsList.url_from_isbn(isbn or catalog_id)
                urls = PublicationsList.from_url(self.browser, query, timeout, log)
                
                add_matches(urls, 1)
                    
            if abort.is_set():
                return
                
            # If we haven't reached the maximum number of results, also search by title and author
            if len(matches) < self._max_results():
                #title = get_udc().decode(title)
                authors = authors or []
                #authors = [get_udc().decode(a) for a in authors]
                
                title_tokens = self.get_title_tokens(title, strip_joiners=False, strip_subtitle=True)
                author_tokens = self.get_author_tokens(authors, only_first_author=True)
                
                query = PublicationsList.url_from_title_and_authors(title_tokens, author_tokens)
                urls = PublicationsList.from_url(self.browser, query, timeout, log)
                
                add_matches(urls, 2)

        if abort.is_set():
            return

        workers = [Worker(m, result_queue, self.browser, log, relevance[m], self) for m in matches]

        for w in workers:
            w.start()
            # Don't send all requests at the same time
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break
        
        return None

    def download_cover(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30):
        cached_url = self.get_cached_cover_url(identifiers)
        
        if not cached_url:
            # TODO: fetch the covers from the title covers page instead!
            # how do we get from publication to title?
            # cache a title identifier from the publication details if it exists
            # but check if the name / author match
            # otherwise do a title query with exact title and author name
            # and filter out only book results (NOVEL, ANTHOLOGY, COLLECTION???)
            # (what about anthologies? what gets entered as the author name?)
            # then take the first result, parse out title ids, and pass to workers
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors, identifiers=identifiers)
            
            if abort.is_set():
                return

            results = []

            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break

            results.sort(key=self.identify_results_keygen(title=title, authors=authors, identifiers=identifiers))

            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
                    
        if not cached_url:
            log.info('No cover found')
            return

        if abort.is_set():
            return
        
        log.info('Downloading cover from:', cached_url)

        try:
            cdata = self.browser.open_novisit(cached_url, timeout=timeout).read()
            result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)

# TODO we can probably eliminate this and use a plain thread object
class Worker(Thread):
    '''
    Get book details from ISFDB book page in a separate thread.
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url = url
        self.result_queue = result_queue
        self.log = log
        self.timeout = timeout
        self.relevance = relevance
        self.plugin = plugin
        self.browser = browser.clone_browser()

    def run(self):
        try:
            self.log.info('Worker parsing ISFDB url: %r' % self.url)

            pub = Publication.from_url(self.browser, self.url, self.timeout, self.log)

            if not pub.get("title") or not pub.get("authors"):
                self.log.error('Insufficient metadata found for %r' % self.url)
                return

            mi = Metadata(pub["title"], pub["authors"])
            
            for id_name in ("isfdb", "isfdb-catalog", "isfdb-title"):
                if id_name in pub:
                    mi.set_identifier(id_name, pub[id_name])
            
            for attr in ("isbn", "publisher", "pubdate", "cover_url", "comments"):
                if attr in pub:
                    setattr(mi, attr, pub[attr])
            
            if pub.get("cover_url"):
                self.plugin.cache_identifier_to_cover_url(pub["isfdb"], pub["cover_url"])
                mi.has_cover = True

            mi.source_relevance = self.relevance

            # TODO: do we actually want / need this?
            if pub.get("isfdb") and pub.get("isbn"):
                self.plugin.cache_isbn_to_identifier(pub["isbn"], pub["isfdb"])

            self.plugin.clean_downloaded_metadata(mi)
            self.result_queue.put(mi)
        except Exception as e:
            self.log.exception('Worker failed to fetch and parse url %r with error %r' % (self.url, e))


if __name__ == '__main__': # tests
    # To run these test use:
    # calibre-debug -e __init__.py
    from calibre import prints
    from calibre.ebooks.metadata.sources.test import (test_identify_plugin, title_test, authors_test)

    def cover_test(cover_url):
        if cover_url is not None:
            cover_url = cover_url.lower()

        def test(mi):
            mc = mi.cover_url
            if mc is not None:
                mc = mc.lower()
            if mc == cover_url:
                return True
            prints('Cover test failed. Expected: \'%s\' found: ' % cover_url, mc)
            return False
        return test

    # Test the plugin.
    # TODO: new test cases
    # by id
    # by catalog id
    # by title id
    # by isbn
    # by author / title
    # anthology
    # with cover
    # without cover
    test_identify_plugin(ISFDB.name,
        [
            #(# A book with an ISBN
                #{'identifiers':{'isbn': '9780345470638'},
                    #'title':'Black House', 'authors':['Stephen King', 'Peter Straub']},
                #[title_test('Black House', exact=True),
                 #authors_test(['Stephen King', 'Peter Straub']),
                 #cover_test('http://images.amazon.com/images/P/034547063X.01.LZZZZZZZ.jpg')]
            #),

            #(# A book with no ISBN specified
                #{'title':'Black House', 'authors':['Stephen King', 'Peter Straub']},
                #[title_test('Black House', exact=True),
                 #authors_test(['Stephen King', 'Peter Straub']),
                 #cover_test('http://images.amazon.com/images/P/034547063X.01.LZZZZZZZ.jpg')]
            #),

            #(# A book with an ISFDB ID
                #{'identifiers':{'isfdb': '4638'},
                    #'title':'Black House', 'authors':['Stephen King', 'Peter Straub']},
                #[title_test('Black House', exact=True),
                 #authors_test(['Stephen King', 'Peter Straub']),
                 #cover_test('http://images.amazon.com/images/P/034547063X.01.LZZZZZZZ.jpg')]
            #)
        ], fail_missing_meta=False)
