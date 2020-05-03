#coding: utf-8
import re

import logging
from urllib.parse import urljoin
from collections import defaultdict
from itertools import chain
from math import sqrt
from bs4 import BeautifulSoup as BS, Tag, NavigableString

from null import Null
from .utils import tokenize, string_inclusion_ratio
from .webimage import WebImage
from backports.functools_lru_cache import lru_cache
from markupsafe import escape

logger = logging.getLogger(__name__)

# Beautifulsoup will convert all tag names to lower-case
ignored_tags = ('option', 'script', 'noscript', 'style', 'iframe', 'head')
block_tags = {'article', 'header', 'aside', 'hgroup', 'blockquote', 'hr',
    'body', 'li', 'br', 'map', 'button', 'object', 'canvas', 'ol', 'caption',
    'output', 'col', 'p', 'colgroup', 'pre', 'dd', 'progress', 'div', 'section',
    'dl', 'table', 'dt', 'tbody', 'embed', 'textarea', 'fieldset', 'tfoot', 'figcaption',
    'th', 'figure', 'thead', 'footer', 'tr', 'form', 'ul', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'video', 'td'}
negative_patt = re.compile(r'comment|combx|disqus|foot|header|menu|rss|'
    'shoutbox|sidebar|sponsor|vote|meta|shar|ad-', re.IGNORECASE)
positive_patt = re.compile(r'article|entry|post|column|main|content|'
    'section|text|preview|view|story-body', re.IGNORECASE)

def tag_equal(self, other):
    return id(self) == id(other)
# Use tag as keys in dom scores,
# two tags with the same content and attributes should not consider equal to each other.
# Tag.__eq__ = tag_equal

class HtmlContentExtractor(object):
    """
    see https://github.com/scyclops/Readable-Feeds/blob/master/readability/hn.py
    """
    def __init__(self, html, url=''):
        # see http://stackoverflow.com/questions/14946264/python-lru-cache-decorator-per-instance
        self.calc_img_area_len = lru_cache(1024)(self.calc_img_area_len)
        # self.calc_effective_text_len = lru_cache(1024)(self.calc_effective_text_len)

        self.max_score = -1
        # dict uses __eq__ to identify key, while in BS two different nodes
        # will also be considered equal, DO not use that
        self.scores = defaultdict(int)
        self.doc = BS(html, 'html.parser')

        self.title = (self.doc.title.string if self.doc.title else u'') or u''
        self.article = Null
        self.url = url
        # call it before purge
        self.get_favicon_url()
        self.get_meta_description()
        self.get_meta_image()
        self.purge()
        self.find_main_content()

        # clean ups
        # self.clean_up_html()
        self.relative_path2_abs_url()

    # def __del__(self):
    #     # TODO won't call
    #     logger.info('calc_effective_text_len: %s, parents_of_article_header: %s, calc_img_area_len: %s',
    #         self.calc_effective_text_len.cache_info(),
    #         self.parents_of_article_header.cache_info(),
    #         self.calc_img_area_len.cache_info())

    def set_title_parents_point(self, doc):
        # First we give a high point to nodes who have
        # a descendant that is a header tag and matches title most
        def is_article_header(node):
            if re.match(r'h\d+|td', node.name, re.I):
                if string_inclusion_ratio(node.text, self.title) > .85:
                    return True
            return False

        for node in doc.find_all(is_article_header):
            # Give eligible node a high score
            logger.info('Found an eligible title: %s', node.text.strip())
            # self.scores[node] = 1000
            for parent in node.parents:
                if not parent or parent is doc:
                    break
                parent.score = parent.score or 0 + \
                           self.calc_effective_text_len(parent) * sqrt(len(node.text))

    def set_article_tag_point(self, doc):
        for node in doc.find_all('article'):
            # Should be less than most titles but better than short ones
            node.score = node.score or 0 + self.calc_effective_text_len(node) * 2

    def calc_node_score(self, node, depth=.1):
        """
        The one with most text is the most likely article, naive and simple
        """
        text_len = self.calc_effective_text_len(node)
        # img_len = self.calc_img_area_len(cur_node)
        #TODO take image as a factor
        img_len = 0
        impact_factor = 2 if self.has_positive_effect(node) else 1
        node.score = (node.score or 0 + text_len + img_len) * impact_factor * (depth**1.5)
        if node.score > self.max_score:
            self.max_score = node.score
            self.article = node

        for child in node.children:  # the direct children, not descendants
            if isinstance(child, Tag):
                self.calc_node_score(child, depth+0.1)

    def find_main_content(self):
        self.calc_effective_text_len(self.doc)
        self.set_title_parents_point(self.doc)  # Give them the highest score
        self.set_article_tag_point(self.doc)

        self.calc_node_score(self.doc)
        logger.info('Score of the main content is %s', self.article.score or 0)

    def get_meta_description(self):
        if not hasattr(self, '_meta_desc'):
            self._meta_desc = ''
            descs = self.doc.find_all('meta', attrs={'name': re.compile('description', re.I)})
            if descs:
                self._meta_desc = descs[-1].get('content', '')
        return self._meta_desc

    def get_meta_image(self):
        if not hasattr(self, '_meta_image'):
            self._meta_image = None
            og_images = self.doc.find_all('meta', property=re.compile('og:image', re.I))
            if og_images:
                self._meta_image = og_images[0].get('content', None)
        return self._meta_image

    @staticmethod
    def has_positive_effect(node):
        for attr in node.get('id', ''), node.name, ' '.join(node.get('class', [])):
            if positive_patt.search(attr):
                return True
        return False

    @staticmethod
    def has_negative_effect(node):
        for attr in node.get('id', ''), node.name, ' '.join(node.get('class', [])):
            if negative_patt.search(attr):
                return True
        return False

    def calc_effective_text_len(self, node):
        """
        Calc the total the length of text in a child, same as
        sum(len(s) for s in cur_node.stripped_strings)
        """
        if node.text_len is not None:
            return node.text_len
        text_len = 0
        for child in node.children:
            if isinstance(child, Tag):
                if child.name == 'a':
                    continue
                text_len += self.calc_effective_text_len(child)
            # Comment is also an instance of NavigableString,
            # so we should not use isinstance(child, NavigableString)
            elif type(child) is NavigableString:
                text_len += len(child.string.strip()) + child.string.count(',') + \
                            child.string.count(u'，')  # Chinese comma
        node.text_len = text_len * .2 if self.has_negative_effect(node) else text_len
        return node.text_len

    def calc_img_area_len(self, cur_node):
        return 0
        # img_len = 0
        # if cur_node.name == 'img':
        #     img_len = WebImage.cached_builder(self.url, cur_node).to_text_len()
        # else:
        #     for node in cur_node.find_all('img', recursive=False):  # only search children first
        #         img_len += self.calc_img_area_len(node)
        # return img_len

    def purge(self):
        for tname in ignored_tags:
            for d in self.doc.find_all(tname):
                d.extract()  # decompose calls extract with some more steps
        for style_links in self.doc.find_all('link', attrs={'type': 'text/css'}):
            style_links.extract()

    def clean_up_html(self):
        trashcan = []
        for tag in self.article.descendants:
            if isinstance(tag, Tag):
                del tag['class']
                del tag['id']
            # <!-- comment -->
            elif isinstance(tag, NavigableString) and type(tag) is not NavigableString:
                # Definitely should not modify the iter while looping
                # tag.extract()
                trashcan.append(tag)
        for t in trashcan:
            t.extract()

    def relative_path2_abs_url(self):
        def _rp2au(soup, tp):
            d = {tp: True}
            for tag in soup.find_all(**d):
                tag[tp] = urljoin(self.url, tag[tp])
        _rp2au(self.article, 'href')
        _rp2au(self.article, 'src')
        _rp2au(self.article, 'background')

    @staticmethod
    def is_link_intensive(node):
        all_text = len(node.get_text(separator=u'', strip=True, types=(NavigableString,)))
        if not all_text:
            return False
        link_text = 0
        for a in node.find_all('a'):
            link_text += len(a.get_text(separator=u'', strip=True, types=(NavigableString,)))
        return float(link_text) / all_text >= .65

    @staticmethod
    def cut_content_to_length(node, length):
        cur_length = 0
        ret = ['<%s>' % node.name]
        for child in node.children:
            if isinstance(child, Tag):
                cs, cl = HtmlContentExtractor.cut_content_to_length(child, length-cur_length)
                ret.append(cs)
                cur_length += cl
            else:
                t = []
                for line in unicode(child).split('\n'):
                    t.append(line)
                    cur_length += len(t[-1])
                    if cur_length >= length:
                        break
                ret.append(escape('\n'.join(t)))
            if cur_length >= length:
                break
        if len(ret) == 1:  # no children
            return unicode(node), 0
        ret.append('</%s>' % node.name)
        return ''.join(ret), cur_length

    def get_summary(self, max_length=300):
        preserved_tags = {'pre'}

        def is_meta_tag(node):
            for attr in chain(node.get('class', []), [node.get('id', '')], [node.name]):
                if re.search(r'meta|date|time|author|share|caption|attr|title|header|summary|'
                             'clear|tag|manage|info|social|avatar|small|sidebar|views|'
                            'created|name|related|nav|pull',
                             attr, re.I):
                    return True
            return False

        def summarize(node, max_length):
            partial_summaries = []

            for child in node.children:
                if isinstance(child, Tag):
                    # Put a space between two blocks
                    partial_summaries.append(' ')  # http://paulgraham.com/know.html
                    # if self.summary_begun:  # http://v2ex.com/t/152930
                    if is_meta_tag(child) and \
                            1.0*self.calc_effective_text_len(child)/self.calc_effective_text_len(self.article) < .3 and \
                            self.calc_effective_text_len(child) < max_length:
                        continue
                    if child.name in block_tags:
                        # Ignore too many links and too short paragraphs
                        if self.is_link_intensive(child) or (len(tokenize(child.text)) < 15 and
                                1.0*self.calc_effective_text_len(child)/self.calc_effective_text_len(self.article) < .3):
                            continue
                        child_summary = summarize(child, max_length).strip()
                        if len(tokenize(child_summary)) < 15 and \
                                1.0*self.calc_effective_text_len(child)/self.calc_effective_text_len(self.article) < .3:
                             continue
                        partial_summaries.append(child_summary)
                    else:
                        partial_summaries.append(summarize(child, max_length))
                    max_length -= len(partial_summaries[-1])
                    if max_length < 0:
                        break
                elif type(child) is NavigableString:
                    # if not child.strip():
                    #     continue
                    if re.match(r'h\d+|td', child.parent.name, re.I) and \
                            string_inclusion_ratio(child, self.title) > .85:
                        continue
                    self.summary_begun = True
                    child = re.sub(u'[ 　]{2,}', u' ', child)  # squeeze spaces
                    if len(child) > max_length:
                        for word in tokenize(child):
                            partial_summaries.append(escape(word))
                            max_length -= len(partial_summaries[-1])
                            if max_length < 0:
                                partial_summaries.append(' ...')
                                return ''.join(partial_summaries)
                    else:
                        partial_summaries.append(escape(child))
                        max_length -= len(partial_summaries[-1])
            return ''.join(partial_summaries)

        self.summary_begun = False  # miss the nonlocal feature
        smr = u''
        if self.calc_effective_text_len(self.article):
            smr = summarize(self.article, max_length).strip()
        if len(smr) <= len(self.get_meta_description()):
            logger.info('Calculated summary is shorter than meta description(%s)', self.url)
            return self.get_meta_description()
        return smr

    def get_illustration(self):
        for img_node in self.article.find_all('img') + self.doc.find_all('img'):
            img = WebImage.from_node(self.url, img_node)
            if img.is_candidate:
                logger.info('Found a top image %s', img.url)
                return img
        # Only as a fall back, github use user's avatar as their meta_images
        if self.get_meta_image():
            img = WebImage.from_attrs(src=self.get_meta_image(), referrer=self.url)
            if img.is_candidate:
                logger.info('Found a meta image %s', img.url)
                return img
        logger.info('No top image is found on %s', self.url)
        return None

    def get_favicon_url(self):
        if not hasattr(self, '_favicon_url'):
            fa = self.doc.find('link', rel=re.compile('icon', re.I))
            favicon_path = fa.get('href', '/favicon.ico') if fa else '/favicon.ico'
            self._favicon_url = urljoin(self.url, favicon_path)
        return self._favicon_url

