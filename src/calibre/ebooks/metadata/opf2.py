#!/usr/bin/env  python
__license__   = 'GPL v3'
__copyright__ = '2008, Kovid Goyal kovid@kovidgoyal.net'
__docformat__ = 'restructuredtext en'

'''
lxml based OPF parser.
'''

import re, sys, unittest, functools, os, uuid, glob, cStringIO, json, copy
from urllib import unquote
from urlparse import urlparse

from lxml import etree

from calibre.ebooks.chardet import xml_to_unicode
from calibre.constants import __appname__, __version__, filesystem_encoding
from calibre.ebooks.metadata.toc import TOC
from calibre.ebooks.metadata import string_to_authors, MetaInformation, check_isbn
from calibre.ebooks.metadata.book.base import Metadata
from calibre.utils.date import parse_date, isoformat
from calibre.utils.localization import get_lang, canonicalize_lang
from calibre import prints, guess_type
from calibre.utils.cleantext import clean_ascii_chars, clean_xml_chars
from calibre.utils.config import tweaks

pretty_print_opf = False

class PrettyPrint(object):

    def __enter__(self):
        global pretty_print_opf
        pretty_print_opf = True

    def __exit__(self, *args):
        global pretty_print_opf
        pretty_print_opf = False
pretty_print = PrettyPrint()

def _pretty_print(root):
    from calibre.ebooks.oeb.polish.pretty import pretty_opf, pretty_xml_tree
    pretty_opf(root)
    pretty_xml_tree(root)

class Resource(object):  # {{{

    '''
    Represents a resource (usually a file on the filesystem or a URL pointing
    to the web. Such resources are commonly referred to in OPF files.

    They have the interface:

    :member:`path`
    :member:`mime_type`
    :method:`href`
    '''

    def __init__(self, href_or_path, basedir=os.getcwdu(), is_path=True):
        self.orig = href_or_path
        self._href = None
        self._basedir = basedir
        self.path = None
        self.fragment = ''
        try:
            self.mime_type = guess_type(href_or_path)[0]
        except:
            self.mime_type = None
        if self.mime_type is None:
            self.mime_type = 'application/octet-stream'
        if is_path:
            path = href_or_path
            if not os.path.isabs(path):
                path = os.path.abspath(os.path.join(basedir, path))
            if isinstance(path, str):
                path = path.decode(sys.getfilesystemencoding())
            self.path = path
        else:
            href_or_path = href_or_path
            url = urlparse(href_or_path)
            if url[0] not in ('', 'file'):
                self._href = href_or_path
            else:
                pc = url[2]
                if isinstance(pc, unicode):
                    pc = pc.encode('utf-8')
                pc = pc.decode('utf-8')
                self.path = os.path.abspath(os.path.join(basedir, pc.replace('/', os.sep)))
                self.fragment = url[-1]

    def href(self, basedir=None):
        '''
        Return a URL pointing to this resource. If it is a file on the filesystem
        the URL is relative to `basedir`.

        `basedir`: If None, the basedir of this resource is used (see :method:`set_basedir`).
        If this resource has no basedir, then the current working directory is used as the basedir.
        '''
        if basedir is None:
            if self._basedir:
                basedir = self._basedir
            else:
                basedir = os.getcwdu()
        if self.path is None:
            return self._href
        f = self.fragment.encode('utf-8') if isinstance(self.fragment, unicode) else self.fragment
        frag = '#'+f if self.fragment else ''
        if self.path == basedir:
            return ''+frag
        try:
            rpath = os.path.relpath(self.path, basedir)
        except ValueError:  # On windows path and basedir could be on different drives
            rpath = self.path
        if isinstance(rpath, unicode):
            rpath = rpath.encode('utf-8')
        return rpath.replace(os.sep, '/')+frag

    def set_basedir(self, path):
        self._basedir = path

    def basedir(self):
        return self._basedir

    def __repr__(self):
        return 'Resource(%s, %s)'%(repr(self.path), repr(self.href()))

# }}}

class ResourceCollection(object):  # {{{

    def __init__(self):
        self._resources = []

    def __iter__(self):
        for r in self._resources:
            yield r

    def __len__(self):
        return len(self._resources)

    def __getitem__(self, index):
        return self._resources[index]

    def __bool__(self):
        return len(self._resources) > 0

    def __str__(self):
        resources = map(repr, self)
        return '[%s]'%', '.join(resources)

    def __repr__(self):
        return str(self)

    def append(self, resource):
        if not isinstance(resource, Resource):
            raise ValueError('Can only append objects of type Resource')
        self._resources.append(resource)

    def remove(self, resource):
        self._resources.remove(resource)

    def replace(self, start, end, items):
        'Same as list[start:end] = items'
        self._resources[start:end] = items

    @staticmethod
    def from_directory_contents(top, topdown=True):
        collection = ResourceCollection()
        for spec in os.walk(top, topdown=topdown):
            path = os.path.abspath(os.path.join(spec[0], spec[1]))
            res = Resource.from_path(path)
            res.set_basedir(top)
            collection.append(res)
        return collection

    def set_basedir(self, path):
        for res in self:
            res.set_basedir(path)

# }}}

class ManifestItem(Resource):  # {{{

    @staticmethod
    def from_opf_manifest_item(item, basedir):
        href = item.get('href', None)
        if href:
            res = ManifestItem(href, basedir=basedir, is_path=True)
            mt = item.get('media-type', '').strip()
            if mt:
                res.mime_type = mt
            return res

    @dynamic_property
    def media_type(self):
        def fget(self):
            return self.mime_type
        def fset(self, val):
            self.mime_type = val
        return property(fget=fget, fset=fset)

    def __unicode__(self):
        return u'<item id="%s" href="%s" media-type="%s" />'%(self.id, self.href(), self.media_type)

    def __str__(self):
        return unicode(self).encode('utf-8')

    def __repr__(self):
        return unicode(self)

    def __getitem__(self, index):
        if index == 0:
            return self.href()
        if index == 1:
            return self.media_type
        raise IndexError('%d out of bounds.'%index)

# }}}

class Manifest(ResourceCollection):  # {{{

    @staticmethod
    def from_opf_manifest_element(items, dir):
        m = Manifest()
        for item in items:
            try:
                m.append(ManifestItem.from_opf_manifest_item(item, dir))
                id = item.get('id', '')
                if not id:
                    id = 'id%d'%m.next_id
                m[-1].id = id
                m.next_id += 1
            except ValueError:
                continue
        return m

    @staticmethod
    def from_paths(entries):
        '''
        `entries`: List of (path, mime-type) If mime-type is None it is autodetected
        '''
        m = Manifest()
        for path, mt in entries:
            mi = ManifestItem(path, is_path=True)
            if mt:
                mi.mime_type = mt
            mi.id = 'id%d'%m.next_id
            m.next_id += 1
            m.append(mi)
        return m

    def add_item(self, path, mime_type=None):
        mi = ManifestItem(path, is_path=True)
        if mime_type:
            mi.mime_type = mime_type
        mi.id = 'id%d'%self.next_id
        self.next_id += 1
        self.append(mi)
        return mi.id

    def __init__(self):
        ResourceCollection.__init__(self)
        self.next_id = 1

    def item(self, id):
        for i in self:
            if i.id == id:
                return i

    def id_for_path(self, path):
        path = os.path.normpath(os.path.abspath(path))
        for i in self:
            if i.path and os.path.normpath(i.path) == path:
                return i.id

    def path_for_id(self, id):
        for i in self:
            if i.id == id:
                return i.path

    def type_for_id(self, id):
        for i in self:
            if i.id == id:
                return i.mime_type

# }}}

class Spine(ResourceCollection):  # {{{

    class Item(Resource):

        def __init__(self, idfunc, *args, **kwargs):
            Resource.__init__(self, *args, **kwargs)
            self.is_linear = True
            self.id = idfunc(self.path)
            self.idref = None

        def __repr__(self):
            return 'Spine.Item(path=%r, id=%s, is_linear=%s)' % \
                    (self.path, self.id, self.is_linear)

    @staticmethod
    def from_opf_spine_element(itemrefs, manifest):
        s = Spine(manifest)
        seen = set()
        for itemref in itemrefs:
            idref = itemref.get('idref', None)
            if idref is not None:
                path = s.manifest.path_for_id(idref)
                if path and path not in seen:
                    r = Spine.Item(lambda x:idref, path, is_path=True)
                    r.is_linear = itemref.get('linear', 'yes') == 'yes'
                    r.idref = idref
                    s.append(r)
                    seen.add(path)
        return s

    @staticmethod
    def from_paths(paths, manifest):
        s = Spine(manifest)
        for path in paths:
            try:
                s.append(Spine.Item(s.manifest.id_for_path, path, is_path=True))
            except:
                continue
        return s

    def __init__(self, manifest):
        ResourceCollection.__init__(self)
        self.manifest = manifest

    def replace(self, start, end, ids):
        '''
        Replace the items between start (inclusive) and end (not inclusive) with
        with the items identified by ids. ids can be a list of any length.
        '''
        items = []
        for id in ids:
            path = self.manifest.path_for_id(id)
            if path is None:
                raise ValueError('id %s not in manifest')
            items.append(Spine.Item(lambda x: id, path, is_path=True))
        ResourceCollection.replace(start, end, items)

    def linear_items(self):
        for r in self:
            if r.is_linear:
                yield r.path

    def nonlinear_items(self):
        for r in self:
            if not r.is_linear:
                yield r.path

    def items(self):
        for i in self:
            yield i.path

# }}}

class Guide(ResourceCollection):  # {{{

    class Reference(Resource):

        @staticmethod
        def from_opf_resource_item(ref, basedir):
            title, href, type = ref.get('title', ''), ref.get('href'), ref.get('type')
            res = Guide.Reference(href, basedir, is_path=True)
            res.title = title
            res.type = type
            return res

        def __repr__(self):
            ans = '<reference type="%s" href="%s" '%(self.type, self.href())
            if self.title:
                ans += 'title="%s" '%self.title
            return ans + '/>'

    @staticmethod
    def from_opf_guide(references, base_dir=os.getcwdu()):
        coll = Guide()
        for ref in references:
            try:
                ref = Guide.Reference.from_opf_resource_item(ref, base_dir)
                coll.append(ref)
            except:
                continue
        return coll

    def set_cover(self, path):
        map(self.remove, [i for i in self if 'cover' in i.type.lower()])
        for type in ('cover', 'other.ms-coverimage-standard', 'other.ms-coverimage'):
            self.append(Guide.Reference(path, is_path=True))
            self[-1].type = type
            self[-1].title = ''

# }}}

class MetadataField(object):

    def __init__(self, name, is_dc=True, formatter=None, none_is=None,
            renderer=lambda x: unicode(x)):
        self.name      = name
        self.is_dc     = is_dc
        self.formatter = formatter
        self.none_is   = none_is
        self.renderer  = renderer

    def __real_get__(self, obj, type=None):
        ans = obj.get_metadata_element(self.name)
        if ans is None:
            return None
        ans = obj.get_text(ans)
        if ans is None:
            return ans
        if self.formatter is not None:
            try:
                ans = self.formatter(ans)
            except:
                return None
        if hasattr(ans, 'strip'):
            ans = ans.strip()
        return ans

    def __get__(self, obj, type=None):
        ans = self.__real_get__(obj, type)
        if ans is None:
            ans = self.none_is
        return ans

    def __set__(self, obj, val):
        elem = obj.get_metadata_element(self.name)
        if val is None:
            if elem is not None:
                elem.getparent().remove(elem)
            return
        if elem is None:
            elem = obj.create_metadata_element(self.name, is_dc=self.is_dc)
        obj.set_text(elem, self.renderer(val))

class TitleSortField(MetadataField):

    def __get__(self, obj, type=None):
        c = self.__real_get__(obj, type)
        if c is None:
            matches = obj.title_path(obj.metadata)
            if matches:
                for match in matches:
                    ans = match.get('{%s}file-as'%obj.NAMESPACES['opf'], None)
                    if not ans:
                        ans = match.get('file-as', None)
                    if ans:
                        c = ans
        if not c:
            c = self.none_is
        else:
            c = c.strip()
        return c

    def __set__(self, obj, val):
        MetadataField.__set__(self, obj, val)
        matches = obj.title_path(obj.metadata)
        if matches:
            for match in matches:
                for attr in list(match.attrib):
                    if attr.endswith('file-as'):
                        del match.attrib[attr]

def serialize_user_metadata(metadata_elem, all_user_metadata, tail='\n'+(' '*8)):
    from calibre.utils.config import to_json
    from calibre.ebooks.metadata.book.json_codec import (object_to_unicode,
                                                         encode_is_multiple)

    for name, fm in all_user_metadata.items():
        try:
            fm = copy.copy(fm)
            encode_is_multiple(fm)
            fm = object_to_unicode(fm)
            fm = json.dumps(fm, default=to_json, ensure_ascii=False)
        except:
            prints('Failed to write user metadata:', name)
            import traceback
            traceback.print_exc()
            continue
        meta = metadata_elem.makeelement('meta')
        meta.set('name', 'calibre:user_metadata:'+name)
        meta.set('content', fm)
        meta.tail = tail
        metadata_elem.append(meta)


def dump_dict(cats):
    if not cats:
        cats = {}
    from calibre.ebooks.metadata.book.json_codec import object_to_unicode
    return json.dumps(object_to_unicode(cats), ensure_ascii=False,
            skipkeys=True)

class OPF(object):  # {{{

    MIMETYPE         = 'application/oebps-package+xml'
    PARSER           = etree.XMLParser(recover=True)
    NAMESPACES       = {
                        None: "http://www.idpf.org/2007/opf",
                        'dc': "http://purl.org/dc/elements/1.1/",
                        'opf': "http://www.idpf.org/2007/opf",
                       }
    META             = '{%s}meta' % NAMESPACES['opf']
    xpn = NAMESPACES.copy()
    xpn.pop(None)
    xpn['re'] = 'http://exslt.org/regular-expressions'
    XPath = functools.partial(etree.XPath, namespaces=xpn)
    CONTENT          = XPath('self::*[re:match(name(), "meta$", "i")]/@content')
    TEXT             = XPath('string()')

    metadata_path   = XPath('descendant::*[re:match(name(), "metadata", "i")]')
    metadata_elem_path = XPath(
        'descendant::*[re:match(name(), concat($name, "$"), "i") or (re:match(name(), "meta$", "i") '
        'and re:match(@name, concat("^calibre:", $name, "$"), "i"))]')
    title_path      = XPath('descendant::*[re:match(name(), "title", "i")]')
    authors_path    = XPath('descendant::*[re:match(name(), "creator", "i") and (@role="aut" or @opf:role="aut" or (not(@role) and not(@opf:role)))]')
    bkp_path        = XPath('descendant::*[re:match(name(), "contributor", "i") and (@role="bkp" or @opf:role="bkp")]')
    tags_path       = XPath('descendant::*[re:match(name(), "subject", "i")]')
    isbn_path       = XPath('descendant::*[re:match(name(), "identifier", "i") and '+
                            '(re:match(@scheme, "isbn", "i") or re:match(@opf:scheme, "isbn", "i"))]')
    pubdate_path    = XPath('descendant::*[re:match(name(), "date", "i")]')
    raster_cover_path = XPath('descendant::*[re:match(name(), "meta", "i") and ' +
            're:match(@name, "cover", "i") and @content]')
    identifier_path = XPath('descendant::*[re:match(name(), "identifier", "i")]')
    application_id_path = XPath('descendant::*[re:match(name(), "identifier", "i") and '+
                            '(re:match(@opf:scheme, "calibre|libprs500", "i") or re:match(@scheme, "calibre|libprs500", "i"))]')
    uuid_id_path    = XPath('descendant::*[re:match(name(), "identifier", "i") and '+
                            '(re:match(@opf:scheme, "uuid", "i") or re:match(@scheme, "uuid", "i"))]')
    languages_path  = XPath('descendant::*[local-name()="language"]')

    manifest_path   = XPath('descendant::*[re:match(name(), "manifest", "i")]/*[re:match(name(), "item", "i")]')
    manifest_ppath  = XPath('descendant::*[re:match(name(), "manifest", "i")]')
    spine_path      = XPath('descendant::*[re:match(name(), "spine", "i")]/*[re:match(name(), "itemref", "i")]')
    guide_path      = XPath('descendant::*[re:match(name(), "guide", "i")]/*[re:match(name(), "reference", "i")]')

    publisher       = MetadataField('publisher')
    comments        = MetadataField('description')
    category        = MetadataField('type')
    rights          = MetadataField('rights')
    series          = MetadataField('series', is_dc=False)
    if tweaks['use_series_auto_increment_tweak_when_importing']:
        series_index    = MetadataField('series_index', is_dc=False,
                                        formatter=float, none_is=None)
    else:
        series_index    = MetadataField('series_index', is_dc=False,
                                        formatter=float, none_is=1)
    title_sort      = TitleSortField('title_sort', is_dc=False)
    rating          = MetadataField('rating', is_dc=False, formatter=float)
    publication_type = MetadataField('publication_type', is_dc=False)
    timestamp       = MetadataField('timestamp', is_dc=False,
                                    formatter=parse_date, renderer=isoformat)
    user_categories = MetadataField('user_categories', is_dc=False,
                                    formatter=json.loads,
                                    renderer=dump_dict)
    author_link_map = MetadataField('author_link_map', is_dc=False,
                                formatter=json.loads, renderer=dump_dict)

    def __init__(self, stream, basedir=os.getcwdu(), unquote_urls=True,
            populate_spine=True, try_to_guess_cover=True):
        if not hasattr(stream, 'read'):
            stream = open(stream, 'rb')
        raw = stream.read()
        if not raw:
            raise ValueError('Empty file: '+getattr(stream, 'name', 'stream'))
        self.try_to_guess_cover = try_to_guess_cover
        self.basedir  = self.base_dir = basedir
        self.path_to_html_toc = self.html_toc_fragment = None
        raw, self.encoding = xml_to_unicode(raw, strip_encoding_pats=True,
                resolve_entities=True, assume_utf8=True)
        raw = raw[raw.find('<'):]
        self.root     = etree.fromstring(raw, self.PARSER)
        if self.root is None:
            raise ValueError('Not an OPF file')
        try:
            self.package_version = float(self.root.get('version', None))
        except (AttributeError, TypeError, ValueError):
            self.package_version = 0
        self.metadata = self.metadata_path(self.root)
        if not self.metadata:
            self.metadata = [self.root.makeelement('{http://www.idpf.org/2007/opf}metadata')]
            self.root.insert(0, self.metadata[0])
            self.metadata[0].tail = '\n'
        self.metadata      = self.metadata[0]
        if unquote_urls:
            self.unquote_urls()
        self.manifest = Manifest()
        m = self.manifest_path(self.root)
        if m:
            self.manifest = Manifest.from_opf_manifest_element(m, basedir)
        self.spine = None
        s = self.spine_path(self.root)
        if populate_spine and s:
            self.spine = Spine.from_opf_spine_element(s, self.manifest)
        self.guide = None
        guide = self.guide_path(self.root)
        self.guide = Guide.from_opf_guide(guide, basedir) if guide else None
        self.cover_data = (None, None)
        self.find_toc()
        self.read_user_metadata()

    def read_user_metadata(self):
        self._user_metadata_ = {}
        temp = Metadata('x', ['x'])
        from calibre.utils.config import from_json
        from calibre.ebooks.metadata.book.json_codec import decode_is_multiple
        elems = self.root.xpath('//*[name() = "meta" and starts-with(@name,'
                '"calibre:user_metadata:") and @content]')
        for elem in elems:
            name = elem.get('name')
            name = ':'.join(name.split(':')[2:])
            if not name or not name.startswith('#'):
                continue
            fm = elem.get('content')
            try:
                fm = json.loads(fm, object_hook=from_json)
                decode_is_multiple(fm)
                temp.set_user_metadata(name, fm)
            except:
                prints('Failed to read user metadata:', name)
                import traceback
                traceback.print_exc()
                continue
        self._user_metadata_ = temp.get_all_user_metadata(True)

    def to_book_metadata(self):
        ans = MetaInformation(self)
        for n, v in self._user_metadata_.items():
            ans.set_user_metadata(n, v)

        ans.set_identifiers(self.get_identifiers())

        return ans

    def write_user_metadata(self):
        elems = self.root.xpath('//*[name() = "meta" and starts-with(@name,'
                '"calibre:user_metadata:") and @content]')
        for elem in elems:
            elem.getparent().remove(elem)
        serialize_user_metadata(self.metadata,
                self._user_metadata_)

    def find_toc(self):
        self.toc = None
        try:
            spine = self.XPath('descendant::*[re:match(name(), "spine", "i")]')(self.root)
            toc = None
            if spine:
                spine = spine[0]
                toc = spine.get('toc', None)
            if toc is None and self.guide:
                for item in self.guide:
                    if item.type and item.type.lower() == 'toc':
                        toc = item.path
            if toc is None:
                for item in self.manifest:
                    if 'toc' in item.href().lower():
                        toc = item.path

            if toc is None:
                return
            self.toc = TOC(base_path=self.base_dir)
            is_ncx = getattr(self, 'manifest', None) is not None and \
                     self.manifest.type_for_id(toc) is not None and \
                     'dtbncx' in self.manifest.type_for_id(toc)
            if is_ncx or toc.lower() in ('ncx', 'ncxtoc'):
                path = self.manifest.path_for_id(toc)
                if path:
                    self.toc.read_ncx_toc(path)
                else:
                    f = glob.glob(os.path.join(self.base_dir, '*.ncx'))
                    if f:
                        self.toc.read_ncx_toc(f[0])
            else:
                self.path_to_html_toc, self.html_toc_fragment = \
                    toc.partition('#')[0], toc.partition('#')[-1]
                if not os.access(self.path_to_html_toc, os.R_OK) or \
                        not os.path.isfile(self.path_to_html_toc):
                    self.path_to_html_toc = None
                self.toc.read_html_toc(toc)
        except:
            pass

    def get_text(self, elem):
        return u''.join(self.CONTENT(elem) or self.TEXT(elem))

    def set_text(self, elem, content):
        if elem.tag == self.META:
            elem.attrib['content'] = content
        else:
            elem.text = content

    def itermanifest(self):
        return self.manifest_path(self.root)

    def create_manifest_item(self, href, media_type):
        ids = [i.get('id', None) for i in self.itermanifest()]
        id = None
        for c in xrange(1, sys.maxint):
            id = 'id%d'%c
            if id not in ids:
                break
        if not media_type:
            media_type = 'application/xhtml+xml'
        ans = etree.Element('{%s}item'%self.NAMESPACES['opf'],
                             attrib={'id':id, 'href':href, 'media-type':media_type})
        ans.tail = '\n\t\t'
        return ans

    def replace_manifest_item(self, item, items):
        items = [self.create_manifest_item(*i) for i in items]
        for i, item2 in enumerate(items):
            item2.set('id', item.get('id')+'.%d'%(i+1))
        manifest = item.getparent()
        index = manifest.index(item)
        manifest[index:index+1] = items
        return [i.get('id') for i in items]

    def add_path_to_manifest(self, path, media_type):
        has_path = False
        path = os.path.abspath(path)
        for i in self.itermanifest():
            xpath = os.path.join(self.base_dir, *(i.get('href', '').split('/')))
            if os.path.abspath(xpath) == path:
                has_path = True
                break
        if not has_path:
            href = os.path.relpath(path, self.base_dir).replace(os.sep, '/')
            item = self.create_manifest_item(href, media_type)
            manifest = self.manifest_ppath(self.root)[0]
            manifest.append(item)

    def iterspine(self):
        return self.spine_path(self.root)

    def spine_items(self):
        for item in self.iterspine():
            idref = item.get('idref', '')
            for x in self.itermanifest():
                if x.get('id', None) == idref:
                    yield x.get('href', '')

    def first_spine_item(self):
        items = self.iterspine()
        if not items:
            return None
        idref = items[0].get('idref', '')
        for x in self.itermanifest():
            if x.get('id', None) == idref:
                return x.get('href', None)

    def create_spine_item(self, idref):
        ans = etree.Element('{%s}itemref'%self.NAMESPACES['opf'], idref=idref)
        ans.tail = '\n\t\t'
        return ans

    def replace_spine_items_by_idref(self, idref, new_idrefs):
        items = list(map(self.create_spine_item, new_idrefs))
        spine = self.XPath('/opf:package/*[re:match(name(), "spine", "i")]')(self.root)[0]
        old = [i for i in self.iterspine() if i.get('idref', None) == idref]
        for x in old:
            i = spine.index(x)
            spine[i:i+1] = items

    def create_guide_element(self):
        e = etree.SubElement(self.root, '{%s}guide'%self.NAMESPACES['opf'])
        e.text = '\n        '
        e.tail =  '\n'
        return e

    def remove_guide(self):
        self.guide = None
        for g in self.root.xpath('./*[re:match(name(), "guide", "i")]', namespaces={'re':'http://exslt.org/regular-expressions'}):
            self.root.remove(g)

    def create_guide_item(self, type, title, href):
        e = etree.Element('{%s}reference'%self.NAMESPACES['opf'],
                             type=type, title=title, href=href)
        e.tail='\n'
        return e

    def add_guide_item(self, type, title, href):
        g = self.root.xpath('./*[re:match(name(), "guide", "i")]', namespaces={'re':'http://exslt.org/regular-expressions'})[0]
        g.append(self.create_guide_item(type, title, href))

    def iterguide(self):
        return self.guide_path(self.root)

    def unquote_urls(self):
        def get_href(item):
            raw = unquote(item.get('href', ''))
            if not isinstance(raw, unicode):
                raw = raw.decode('utf-8')
            return raw
        for item in self.itermanifest():
            item.set('href', get_href(item))
        for item in self.iterguide():
            item.set('href', get_href(item))

    @dynamic_property
    def title(self):
        # TODO: Add support for EPUB 3 refinements

        def fget(self):
            for elem in self.title_path(self.metadata):
                title = self.get_text(elem)
                if title and title.strip():
                    return re.sub(r'\s+', ' ', title.strip())

        def fset(self, val):
            val = (val or '').strip()
            titles = self.title_path(self.metadata)
            if self.package_version < 3:
                # EPUB 3 allows multiple title elements containing sub-titles,
                # series and other things. We all loooove EPUB 3.
                for title in titles:
                    title.getparent().remove(title)
                titles = ()
            if val:
                title = titles[0] if titles else self.create_metadata_element('title')
                title.text = re.sub(r'\s+', ' ', unicode(val))

        return property(fget=fget, fset=fset)

    @dynamic_property
    def authors(self):

        def fget(self):
            ans = []
            for elem in self.authors_path(self.metadata):
                ans.extend(string_to_authors(self.get_text(elem)))
            return ans

        def fset(self, val):
            remove = list(self.authors_path(self.metadata))
            for elem in remove:
                elem.getparent().remove(elem)
            # Ensure new author element is at the top of the list
            # for broken implementations that always use the first
            # <dc:creator> element with no attention to the role
            for author in reversed(val):
                elem = self.metadata.makeelement('{%s}creator'%
                        self.NAMESPACES['dc'], nsmap=self.NAMESPACES)
                elem.tail = '\n'
                self.metadata.insert(0, elem)
                elem.set('{%s}role'%self.NAMESPACES['opf'], 'aut')
                self.set_text(elem, author.strip())

        return property(fget=fget, fset=fset)

    @dynamic_property
    def author_sort(self):

        def fget(self):
            matches = self.authors_path(self.metadata)
            if matches:
                for match in matches:
                    ans = match.get('{%s}file-as'%self.NAMESPACES['opf'], None)
                    if not ans:
                        ans = match.get('file-as', None)
                    if ans:
                        return ans

        def fset(self, val):
            matches = self.authors_path(self.metadata)
            if matches:
                for key in matches[0].attrib:
                    if key.endswith('file-as'):
                        matches[0].attrib.pop(key)
                matches[0].set('{%s}file-as'%self.NAMESPACES['opf'], unicode(val))

        return property(fget=fget, fset=fset)

    @dynamic_property
    def tags(self):

        def fget(self):
            ans = []
            for tag in self.tags_path(self.metadata):
                text = self.get_text(tag)
                if text and text.strip():
                    ans.extend([x.strip() for x in text.split(',')])
            return ans

        def fset(self, val):
            for tag in list(self.tags_path(self.metadata)):
                tag.getparent().remove(tag)
            for tag in val:
                elem = self.create_metadata_element('subject')
                self.set_text(elem, unicode(tag))

        return property(fget=fget, fset=fset)

    @dynamic_property
    def pubdate(self):

        def fget(self):
            ans = None
            for match in self.pubdate_path(self.metadata):
                try:
                    val = parse_date(etree.tostring(match, encoding=unicode,
                        method='text', with_tail=False).strip())
                except:
                    continue
                if ans is None or val < ans:
                    ans = val
            return ans

        def fset(self, val):
            least_val = least_elem = None
            for match in self.pubdate_path(self.metadata):
                try:
                    cval = parse_date(etree.tostring(match, encoding=unicode,
                        method='text', with_tail=False).strip())
                except:
                    match.getparent().remove(match)
                else:
                    if not val:
                        match.getparent().remove(match)
                    if least_val is None or cval < least_val:
                        least_val, least_elem = cval, match

            if val:
                if least_val is None:
                    least_elem = self.create_metadata_element('date')

                least_elem.attrib.clear()
                least_elem.text = isoformat(val)

        return property(fget=fget, fset=fset)

    @dynamic_property
    def isbn(self):

        def fget(self):
            for match in self.isbn_path(self.metadata):
                return self.get_text(match) or None

        def fset(self, val):
            matches = self.isbn_path(self.metadata)
            if not val:
                for x in matches:
                    x.getparent().remove(x)
                return
            if not matches:
                attrib = {'{%s}scheme'%self.NAMESPACES['opf']: 'ISBN'}
                matches = [self.create_metadata_element('identifier',
                                                        attrib=attrib)]
            self.set_text(matches[0], unicode(val))

        return property(fget=fget, fset=fset)

    def get_identifiers(self):
        identifiers = {}
        for x in self.XPath(
            'descendant::*[local-name() = "identifier" and text()]')(
                    self.metadata):
            found_scheme = False
            for attr, val in x.attrib.iteritems():
                if attr.endswith('scheme'):
                    typ = icu_lower(val)
                    val = etree.tostring(x, with_tail=False, encoding=unicode,
                            method='text').strip()
                    if val and typ not in ('calibre', 'uuid'):
                        if typ == 'isbn' and val.lower().startswith('urn:isbn:'):
                            val = val[len('urn:isbn:'):]
                        identifiers[typ] = val
                    found_scheme = True
                    break
            if not found_scheme:
                val = etree.tostring(x, with_tail=False, encoding=unicode,
                            method='text').strip()
                if val.lower().startswith('urn:isbn:'):
                    val = check_isbn(val.split(':')[-1])
                    if val is not None:
                        identifiers['isbn'] = val
        return identifiers

    def set_identifiers(self, identifiers):
        identifiers = identifiers.copy()
        uuid_id = None
        for attr in self.root.attrib:
            if attr.endswith('unique-identifier'):
                uuid_id = self.root.attrib[attr]
                break

        for x in self.XPath(
            'descendant::*[local-name() = "identifier"]')(
                    self.metadata):
            xid = x.get('id', None)
            is_package_identifier = uuid_id is not None and uuid_id == xid
            typ = {val for attr, val in x.attrib.iteritems() if attr.endswith('scheme')}
            if is_package_identifier:
                typ = tuple(typ)
                if typ and typ[0].lower() in identifiers:
                    self.set_text(x, identifiers.pop(typ[0].lower()))
                continue
            if typ and not (typ & {'calibre', 'uuid'}):
                x.getparent().remove(x)

        for typ, val in identifiers.iteritems():
            attrib = {'{%s}scheme'%self.NAMESPACES['opf']: typ.upper()}
            self.set_text(self.create_metadata_element(
                'identifier', attrib=attrib), unicode(val))

    @dynamic_property
    def application_id(self):

        def fget(self):
            for match in self.application_id_path(self.metadata):
                return self.get_text(match) or None

        def fset(self, val):
            removed_ids = set()
            for x in tuple(self.application_id_path(self.metadata)):
                removed_ids.add(x.get('id', None))
                x.getparent().remove(x)

            uuid_id = None
            for attr in self.root.attrib:
                if attr.endswith('unique-identifier'):
                    uuid_id = self.root.attrib[attr]
                    break
            attrib = {'{%s}scheme'%self.NAMESPACES['opf']: 'calibre'}
            if uuid_id and uuid_id in removed_ids:
                attrib['id'] = uuid_id
            self.set_text(self.create_metadata_element(
                'identifier', attrib=attrib), unicode(val))

        return property(fget=fget, fset=fset)

    @dynamic_property
    def uuid(self):

        def fget(self):
            for match in self.uuid_id_path(self.metadata):
                return self.get_text(match) or None

        def fset(self, val):
            matches = self.uuid_id_path(self.metadata)
            if not matches:
                attrib = {'{%s}scheme'%self.NAMESPACES['opf']: 'uuid'}
                matches = [self.create_metadata_element('identifier',
                                                        attrib=attrib)]
            self.set_text(matches[0], unicode(val))

        return property(fget=fget, fset=fset)

    @dynamic_property
    def language(self):

        def fget(self):
            ans = self.languages
            if ans:
                return ans[0]

        def fset(self, val):
            self.languages = [val]

        return property(fget=fget, fset=fset)

    @dynamic_property
    def languages(self):

        def fget(self):
            ans = []
            for match in self.languages_path(self.metadata):
                t = self.get_text(match)
                if t and t.strip():
                    l = canonicalize_lang(t.strip())
                    if l:
                        ans.append(l)
            return ans

        def fset(self, val):
            matches = self.languages_path(self.metadata)
            for x in matches:
                x.getparent().remove(x)

            for lang in val:
                l = self.create_metadata_element('language')
                self.set_text(l, unicode(lang))

        return property(fget=fget, fset=fset)

    @property
    def raw_languages(self):
        for match in self.languages_path(self.metadata):
            t = self.get_text(match)
            if t and t.strip():
                yield t.strip()

    @dynamic_property
    def book_producer(self):

        def fget(self):
            for match in self.bkp_path(self.metadata):
                return self.get_text(match) or None

        def fset(self, val):
            matches = self.bkp_path(self.metadata)
            if not matches:
                matches = [self.create_metadata_element('contributor')]
                matches[0].set('{%s}role'%self.NAMESPACES['opf'], 'bkp')
            self.set_text(matches[0], unicode(val))
        return property(fget=fget, fset=fset)

    def identifier_iter(self):
        for item in self.identifier_path(self.metadata):
            yield item

    @property
    def raw_unique_identifier(self):
        uuid_elem = None
        for attr in self.root.attrib:
            if attr.endswith('unique-identifier'):
                uuid_elem = self.root.attrib[attr]
                break
        if uuid_elem:
            matches = self.root.xpath('//*[@id=%r]'%uuid_elem)
            if matches:
                for m in matches:
                    raw = m.text
                    if raw:
                        return raw

    @property
    def unique_identifier(self):
        raw = self.raw_unique_identifier
        if raw:
            return raw.rpartition(':')[-1]

    @property
    def page_progression_direction(self):
        spine = self.XPath('descendant::*[re:match(name(), "spine", "i")][1]')(self.root)
        if spine:
            for k, v in spine[0].attrib.iteritems():
                if k == 'page-progression-direction' or k.endswith('}page-progression-direction'):
                    return v

    def guess_cover(self):
        '''
        Try to guess a cover. Needed for some old/badly formed OPF files.
        '''
        if self.base_dir and os.path.exists(self.base_dir):
            for item in self.identifier_path(self.metadata):
                scheme = None
                for key in item.attrib.keys():
                    if key.endswith('scheme'):
                        scheme = item.get(key)
                        break
                if scheme is None:
                    continue
                if item.text:
                    prefix = item.text.replace('-', '')
                    for suffix in ['.jpg', '.jpeg', '.gif', '.png', '.bmp']:
                        cpath = os.access(os.path.join(self.base_dir, prefix+suffix), os.R_OK)
                        if os.access(os.path.join(self.base_dir, prefix+suffix), os.R_OK):
                            return cpath

    @property
    def raster_cover(self):
        covers = self.raster_cover_path(self.metadata)
        if covers:
            cover_id = covers[0].get('content')
            for item in self.itermanifest():
                if item.get('id', None) == cover_id:
                    mt = item.get('media-type', '')
                    if mt and 'xml' not in mt and 'html' not in mt:
                        return item.get('href', None)
            for item in self.itermanifest():
                if item.get('href', None) == cover_id:
                    mt = item.get('media-type', '')
                    if mt and mt.startswith('image/'):
                        return item.get('href', None)

    @dynamic_property
    def cover(self):

        def fget(self):
            if self.guide is not None:
                for t in ('cover', 'other.ms-coverimage-standard', 'other.ms-coverimage'):
                    for item in self.guide:
                        if item.type and item.type.lower() == t:
                            return item.path
            try:
                if self.try_to_guess_cover:
                    return self.guess_cover()
            except:
                pass

        def fset(self, path):
            if self.guide is not None:
                self.guide.set_cover(path)
                for item in list(self.iterguide()):
                    if 'cover' in item.get('type', ''):
                        item.getparent().remove(item)

            else:
                g = self.create_guide_element()
                self.guide = Guide()
                self.guide.set_cover(path)
                etree.SubElement(g, 'opf:reference', nsmap=self.NAMESPACES,
                                 attrib={'type':'cover', 'href':self.guide[-1].href()})
            id = self.manifest.id_for_path(self.cover)
            if id is None:
                for t in ('cover', 'other.ms-coverimage-standard', 'other.ms-coverimage'):
                    for item in self.guide:
                        if item.type.lower() == t:
                            self.create_manifest_item(item.href(), guess_type(path)[0])

        return property(fget=fget, fset=fset)

    def get_metadata_element(self, name):
        matches = self.metadata_elem_path(self.metadata, name=name)
        if matches:
            return matches[-1]

    def create_metadata_element(self, name, attrib=None, is_dc=True):
        if is_dc:
            name = '{%s}%s' % (self.NAMESPACES['dc'], name)
        else:
            attrib = attrib or {}
            attrib['name'] = 'calibre:' + name
            name = '{%s}%s' % (self.NAMESPACES['opf'], 'meta')
        nsmap = dict(self.NAMESPACES)
        del nsmap['opf']
        elem = etree.SubElement(self.metadata, name, attrib=attrib,
                                nsmap=nsmap)
        elem.tail = '\n'
        return elem

    def render(self, encoding='utf-8'):
        for meta in self.raster_cover_path(self.metadata):
            # Ensure that the name attribute occurs before the content
            # attribute. Needed for Nooks.
            a = meta.attrib
            c = a.get('content', None)
            if c is not None:
                del a['content']
                a['content'] = c

        self.write_user_metadata()
        if pretty_print_opf:
            _pretty_print(self.root)
        raw = etree.tostring(self.root, encoding=encoding, pretty_print=True)
        if not raw.lstrip().startswith('<?xml '):
            raw = '<?xml version="1.0"  encoding="%s"?>\n'%encoding.upper()+raw
        return raw

    def smart_update(self, mi, replace_metadata=False, apply_null=False):
        for attr in ('title', 'authors', 'author_sort', 'title_sort',
                     'publisher', 'series', 'series_index', 'rating',
                     'isbn', 'tags', 'category', 'comments', 'book_producer',
                     'pubdate', 'user_categories', 'author_link_map'):
            val = getattr(mi, attr, None)
            is_null = val is None or val in ((), [], (None, None), {})
            if is_null:
                if apply_null and attr in {'series', 'tags', 'isbn', 'comments', 'publisher'}:
                    setattr(self, attr, ([] if attr == 'tags' else None))
            else:
                setattr(self, attr, val)
        langs = getattr(mi, 'languages', [])
        if langs == ['und']:
            langs = []
        if apply_null or langs:
            self.languages = langs or []
        temp = self.to_book_metadata()
        temp.smart_update(mi, replace_metadata=replace_metadata)
        if not replace_metadata and callable(getattr(temp, 'custom_field_keys', None)):
            # We have to replace non-null fields regardless of the value of
            # replace_metadata to match the behavior of the builtin fields
            # above.
            for x in temp.custom_field_keys():
                meta = temp.get_user_metadata(x, make_copy=True)
                if meta is None:
                    continue
                if meta['datatype'] == 'text' and meta['is_multiple']:
                    val = mi.get(x, [])
                    if val or apply_null:
                        temp.set(x, val)
                elif meta['datatype'] in {'int', 'float', 'bool'}:
                    missing = object()
                    val = mi.get(x, missing)
                    if val is missing:
                        if apply_null:
                            temp.set(x, None)
                    elif apply_null or val is not None:
                        temp.set(x, val)
                elif apply_null and mi.is_null(x) and not temp.is_null(x):
                    temp.set(x, None)

        self._user_metadata_ = temp.get_all_user_metadata(True)

# }}}

class OPFCreator(Metadata):

    def __init__(self, base_path, other):
        '''
        Initialize.
        @param base_path: An absolute path to the directory in which this OPF file
        will eventually be. This is used by the L{create_manifest} method
        to convert paths to files into relative paths.
        '''
        Metadata.__init__(self, title='', other=other)
        self.base_path = os.path.abspath(base_path)
        self.page_progression_direction = None
        if self.application_id is None:
            self.application_id = str(uuid.uuid4())
        if not isinstance(self.toc, TOC):
            self.toc = None
        if not self.authors:
            self.authors = [_('Unknown')]
        if self.guide is None:
            self.guide = Guide()
        if self.cover:
            self.guide.set_cover(self.cover)

    def create_manifest(self, entries):
        '''
        Create <manifest>

        `entries`: List of (path, mime-type) If mime-type is None it is autodetected
        '''
        entries = map(lambda x: x if os.path.isabs(x[0]) else
                      (os.path.abspath(os.path.join(self.base_path, x[0])), x[1]),
                      entries)
        self.manifest = Manifest.from_paths(entries)
        self.manifest.set_basedir(self.base_path)

    def create_manifest_from_files_in(self, files_and_dirs,
            exclude=lambda x:False):
        entries = []

        def dodir(dir):
            for spec in os.walk(dir):
                root, files = spec[0], spec[-1]
                for name in files:
                    path = os.path.join(root, name)
                    if os.path.isfile(path) and not exclude(path):
                        entries.append((path, None))

        for i in files_and_dirs:
            if os.path.isdir(i):
                dodir(i)
            else:
                entries.append((i, None))

        self.create_manifest(entries)

    def create_spine(self, entries):
        '''
        Create the <spine> element. Must first call :method:`create_manifest`.

        `entries`: List of paths
        '''
        entries = map(lambda x: x if os.path.isabs(x) else
                      os.path.abspath(os.path.join(self.base_path, x)), entries)
        self.spine = Spine.from_paths(entries, self.manifest)

    def set_toc(self, toc):
        '''
        Set the toc. You must call :method:`create_spine` before calling this
        method.

        :param toc: A :class:`TOC` object
        '''
        self.toc = toc

    def create_guide(self, guide_element):
        self.guide = Guide.from_opf_guide(guide_element, self.base_path)
        self.guide.set_basedir(self.base_path)

    def render(self, opf_stream=sys.stdout, ncx_stream=None,
               ncx_manifest_entry=None, encoding=None):
        if encoding is None:
            encoding = 'utf-8'
        toc = getattr(self, 'toc', None)
        if self.manifest:
            self.manifest.set_basedir(self.base_path)
            if ncx_manifest_entry is not None and toc is not None:
                if not os.path.isabs(ncx_manifest_entry):
                    ncx_manifest_entry = os.path.join(self.base_path, ncx_manifest_entry)
                remove = [i for i in self.manifest if i.id == 'ncx']
                for item in remove:
                    self.manifest.remove(item)
                self.manifest.append(ManifestItem(ncx_manifest_entry, self.base_path))
                self.manifest[-1].id = 'ncx'
                self.manifest[-1].mime_type = 'application/x-dtbncx+xml'
        if self.guide is None:
            self.guide = Guide()
        if self.cover:
            cover = self.cover
            if not os.path.isabs(cover):
                cover = os.path.abspath(os.path.join(self.base_path, cover))
            self.guide.set_cover(cover)
        self.guide.set_basedir(self.base_path)

        # Actual rendering
        from lxml.builder import ElementMaker
        from calibre.ebooks.oeb.base import OPF2_NS, DC11_NS, CALIBRE_NS
        DNS = OPF2_NS+'___xx___'
        E = ElementMaker(namespace=DNS, nsmap={None:DNS})
        M = ElementMaker(namespace=DNS,
                nsmap={'dc':DC11_NS, 'calibre':CALIBRE_NS, 'opf':OPF2_NS})
        DC = ElementMaker(namespace=DC11_NS)

        def DC_ELEM(tag, text, dc_attrs={}, opf_attrs={}):
            if text:
                elem = getattr(DC, tag)(clean_ascii_chars(text), **dc_attrs)
            else:
                elem = getattr(DC, tag)(**dc_attrs)
            for k, v in opf_attrs.items():
                elem.set('{%s}%s'%(OPF2_NS, k), v)
            return elem

        def CAL_ELEM(name, content):
            return M.meta(name=name, content=content)

        metadata = M.metadata()
        a = metadata.append
        role = {}
        a(DC_ELEM('title', self.title if self.title else _('Unknown'),
            opf_attrs=role))
        for i, author in enumerate(self.authors):
            fa = {'role':'aut'}
            if i == 0 and self.author_sort:
                fa['file-as'] = self.author_sort
            a(DC_ELEM('creator', author, opf_attrs=fa))
        a(DC_ELEM('contributor', '%s (%s) [%s]'%(__appname__, __version__,
            'http://calibre-ebook.com'), opf_attrs={'role':'bkp',
                'file-as':__appname__}))
        a(DC_ELEM('identifier', str(self.application_id),
            opf_attrs={'scheme':__appname__},
            dc_attrs={'id':__appname__+'_id'}))
        if getattr(self, 'pubdate', None) is not None:
            a(DC_ELEM('date', self.pubdate.isoformat()))
        langs = self.languages
        if not langs or langs == ['und']:
            langs = [get_lang().replace('_', '-').partition('-')[0]]
        for lang in langs:
            a(DC_ELEM('language', lang))
        if self.comments:
            a(DC_ELEM('description', self.comments))
        if self.publisher:
            a(DC_ELEM('publisher', self.publisher))
        for key, val in self.get_identifiers().iteritems():
            a(DC_ELEM('identifier', val, opf_attrs={'scheme':icu_upper(key)}))
        if self.rights:
            a(DC_ELEM('rights', self.rights))
        if self.tags:
            for tag in self.tags:
                a(DC_ELEM('subject', tag))
        if self.series:
            a(CAL_ELEM('calibre:series', self.series))
            if self.series_index is not None:
                a(CAL_ELEM('calibre:series_index', self.format_series_index()))
        if self.title_sort:
            a(CAL_ELEM('calibre:title_sort', self.title_sort))
        if self.rating is not None:
            a(CAL_ELEM('calibre:rating', str(self.rating)))
        if self.timestamp is not None:
            a(CAL_ELEM('calibre:timestamp', self.timestamp.isoformat()))
        if self.publication_type is not None:
            a(CAL_ELEM('calibre:publication_type', self.publication_type))
        if self.user_categories:
            from calibre.ebooks.metadata.book.json_codec import object_to_unicode
            a(CAL_ELEM('calibre:user_categories',
                       json.dumps(object_to_unicode(self.user_categories))))
        manifest = E.manifest()
        if self.manifest is not None:
            for ref in self.manifest:
                item = E.item(id=str(ref.id), href=ref.href())
                item.set('media-type', ref.mime_type)
                manifest.append(item)
        spine = E.spine()
        if self.toc is not None:
            spine.set('toc', 'ncx')
        if self.page_progression_direction is not None:
            spine.set('page-progression-direction', self.page_progression_direction)
        if self.spine is not None:
            for ref in self.spine:
                if ref.id is not None:
                    spine.append(E.itemref(idref=ref.id))
        guide = E.guide()
        if self.guide is not None:
            for ref in self.guide:
                href = ref.href()
                if isinstance(href, bytes):
                    href = href.decode('utf-8')
                item = E.reference(type=ref.type, href=href)
                if ref.title:
                    item.set('title', ref.title)
                guide.append(item)

        serialize_user_metadata(metadata, self.get_all_user_metadata(False))

        root = E.package(
                metadata,
                manifest,
                spine,
                guide
        )
        root.set('unique-identifier', __appname__+'_id')
        raw = etree.tostring(root, pretty_print=True, xml_declaration=True,
                encoding=encoding)
        raw = raw.replace(DNS, OPF2_NS)
        opf_stream.write(raw)
        opf_stream.flush()
        if toc is not None and ncx_stream is not None:
            toc.render(ncx_stream, self.application_id)
            ncx_stream.flush()


def metadata_to_opf(mi, as_string=True, default_lang=None):
    from lxml import etree
    import textwrap
    from calibre.ebooks.oeb.base import OPF, DC

    if not mi.application_id:
        mi.application_id = str(uuid.uuid4())

    if not mi.uuid:
        mi.uuid = str(uuid.uuid4())

    if not mi.book_producer:
        mi.book_producer = __appname__ + ' (%s) '%__version__ + \
            '[http://calibre-ebook.com]'

    if not mi.languages:
        lang = (get_lang().replace('_', '-').partition('-')[0] if default_lang
                is None else default_lang)
        mi.languages = [lang]

    root = etree.fromstring(textwrap.dedent(
    '''
    <package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uuid_id" version="2.0">
        <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
            <dc:identifier opf:scheme="%(a)s" id="%(a)s_id">%(id)s</dc:identifier>
            <dc:identifier opf:scheme="uuid" id="uuid_id">%(uuid)s</dc:identifier>
            </metadata>
        <guide/>
    </package>
    '''%dict(a=__appname__, id=mi.application_id, uuid=mi.uuid)))
    metadata = root[0]
    guide = root[1]
    metadata[0].tail = '\n'+(' '*8)
    def factory(tag, text=None, sort=None, role=None, scheme=None, name=None,
            content=None):
        attrib = {}
        if sort:
            attrib[OPF('file-as')] = sort
        if role:
            attrib[OPF('role')] = role
        if scheme:
            attrib[OPF('scheme')] = scheme
        if name:
            attrib['name'] = name
        if content:
            attrib['content'] = content
        try:
            elem = metadata.makeelement(tag, attrib=attrib)
        except ValueError:
            elem = metadata.makeelement(tag, attrib={k:clean_xml_chars(v) for k, v in attrib.iteritems()})
        elem.tail = '\n'+(' '*8)
        if text:
            try:
                elem.text = text.strip()
            except ValueError:
                elem.text = clean_ascii_chars(text.strip())
        metadata.append(elem)

    factory(DC('title'), mi.title)
    for au in mi.authors:
        factory(DC('creator'), au, mi.author_sort, 'aut')
    factory(DC('contributor'), mi.book_producer, __appname__, 'bkp')
    if hasattr(mi.pubdate, 'isoformat'):
        factory(DC('date'), isoformat(mi.pubdate))
    if hasattr(mi, 'category') and mi.category:
        factory(DC('type'), mi.category)
    if mi.comments:
        factory(DC('description'), clean_ascii_chars(mi.comments))
    if mi.publisher:
        factory(DC('publisher'), mi.publisher)
    for key, val in mi.get_identifiers().iteritems():
        factory(DC('identifier'), val, scheme=icu_upper(key))
    if mi.rights:
        factory(DC('rights'), mi.rights)
    for lang in mi.languages:
        if not lang or lang.lower() == 'und':
            continue
        factory(DC('language'), lang)
    if mi.tags:
        for tag in mi.tags:
            factory(DC('subject'), tag)
    meta = lambda n, c: factory('meta', name='calibre:'+n, content=c)
    if getattr(mi, 'author_link_map', None) is not None:
        meta('author_link_map', dump_dict(mi.author_link_map))
    if mi.series:
        meta('series', mi.series)
    if mi.series_index is not None:
        meta('series_index', mi.format_series_index())
    if mi.rating is not None:
        meta('rating', str(mi.rating))
    if hasattr(mi.timestamp, 'isoformat'):
        meta('timestamp', isoformat(mi.timestamp))
    if mi.publication_type:
        meta('publication_type', mi.publication_type)
    if mi.title_sort:
        meta('title_sort', mi.title_sort)
    if mi.user_categories:
        meta('user_categories', dump_dict(mi.user_categories))

    serialize_user_metadata(metadata, mi.get_all_user_metadata(False))

    metadata[-1].tail = '\n' +(' '*4)

    if mi.cover:
        if not isinstance(mi.cover, unicode):
            mi.cover = mi.cover.decode(filesystem_encoding)
        guide.text = '\n'+(' '*8)
        r = guide.makeelement(OPF('reference'),
                attrib={'type':'cover', 'title':_('Cover'), 'href':mi.cover})
        r.tail = '\n' +(' '*4)
        guide.append(r)
    if pretty_print_opf:
        _pretty_print(root)

    return etree.tostring(root, pretty_print=True, encoding='utf-8',
            xml_declaration=True) if as_string else root


def test_m2o():
    from calibre.utils.date import now as nowf
    from cStringIO import StringIO
    mi = MetaInformation('test & title', ['a"1', "a'2"])
    mi.title_sort = 'a\'"b'
    mi.author_sort = 'author sort'
    mi.pubdate = nowf()
    mi.language = 'en'
    mi.comments = 'what a fun book\n\n'
    mi.publisher = 'publisher'
    mi.set_identifiers({'isbn':'booo', 'dummy':'dummy'})
    mi.tags = ['a', 'b']
    mi.series = 's"c\'l&<>'
    mi.series_index = 3.34
    mi.rating = 3
    mi.timestamp = nowf()
    mi.publication_type = 'ooooo'
    mi.rights = 'yes'
    mi.cover = os.path.abspath('asd.jpg')
    opf = metadata_to_opf(mi)
    print opf
    newmi = MetaInformation(OPF(StringIO(opf)))
    for attr in ('author_sort', 'title_sort', 'comments',
                    'publisher', 'series', 'series_index', 'rating',
                    'isbn', 'tags', 'cover_data', 'application_id',
                    'language', 'cover',
                    'book_producer', 'timestamp',
                    'pubdate', 'rights', 'publication_type'):
        o, n = getattr(mi, attr), getattr(newmi, attr)
        if o != n and o.strip() != n.strip():
            print 'FAILED:', attr, getattr(mi, attr), '!=', getattr(newmi, attr)
    if mi.get_identifiers() != newmi.get_identifiers():
        print 'FAILED:', 'identifiers', mi.get_identifiers(),
        print '!=', newmi.get_identifiers()


class OPFTest(unittest.TestCase):

    def setUp(self):
        self.stream = cStringIO.StringIO(
'''\
<?xml version="1.0"  encoding="UTF-8"?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf" >
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title opf:file-as="Wow">A Cool &amp; &copy; &#223; Title</dc:title>
    <creator opf:role="aut" file-as="Monkey">Monkey Kitchen</creator>
    <creator opf:role="aut">Next</creator>
    <dc:subject>One</dc:subject><dc:subject>Two</dc:subject>
    <dc:identifier scheme="ISBN">123456789</dc:identifier>
    <dc:identifier scheme="dummy">dummy</dc:identifier>
    <meta name="calibre:series" content="A one book series" />
    <meta name="calibre:rating" content="4"/>
    <meta name="calibre:publication_type" content="test"/>
    <meta name="calibre:series_index" content="2.5" />
</metadata>
<manifest>
    <item id="1" href="a%20%7E%20b" media-type="text/txt" />
</manifest>
</package>
'''
        )
        self.opf = OPF(self.stream, os.getcwdu())

    def testReading(self, opf=None):
        if opf is None:
            opf = self.opf
        self.assertEqual(opf.title, u'A Cool & \xa9 \xdf Title')
        self.assertEqual(opf.authors, u'Monkey Kitchen,Next'.split(','))
        self.assertEqual(opf.author_sort, 'Monkey')
        self.assertEqual(opf.title_sort, 'Wow')
        self.assertEqual(opf.tags, ['One', 'Two'])
        self.assertEqual(opf.isbn, '123456789')
        self.assertEqual(opf.series, 'A one book series')
        self.assertEqual(opf.series_index, 2.5)
        self.assertEqual(opf.rating, 4)
        self.assertEqual(opf.publication_type, 'test')
        self.assertEqual(list(opf.itermanifest())[0].get('href'), 'a ~ b')
        self.assertEqual(opf.get_identifiers(), {'isbn':'123456789',
            'dummy':'dummy'})

    def testWriting(self):
        for test in [('title', 'New & Title'), ('authors', ['One', 'Two']),
                     ('author_sort', "Kitchen"), ('tags', ['Three']),
                     ('isbn', 'a'), ('rating', 3), ('series_index', 1),
                     ('title_sort', 'ts')]:
            setattr(self.opf, *test)
            attr, val = test
            self.assertEqual(getattr(self.opf, attr), val)

        self.opf.render()

    def testCreator(self):
        opf = OPFCreator(os.getcwdu(), self.opf)
        buf = cStringIO.StringIO()
        opf.render(buf)
        raw = buf.getvalue()
        self.testReading(opf=OPF(cStringIO.StringIO(raw), os.getcwdu()))

    def testSmartUpdate(self):
        self.opf.smart_update(MetaInformation(self.opf))
        self.testReading()

def suite():
    return unittest.TestLoader().loadTestsFromTestCase(OPFTest)

def test():
    unittest.TextTestRunner(verbosity=2).run(suite())

def test_user_metadata():
    from cStringIO import StringIO
    mi = Metadata('Test title', ['test author1', 'test author2'])
    um = {
        '#myseries': {'#value#': u'test series\xe4', 'datatype':'text',
            'is_multiple': None, 'name': u'My Series'},
        '#myseries_index': {'#value#': 2.45, 'datatype': 'float',
            'is_multiple': None},
        '#mytags': {'#value#':['t1','t2','t3'], 'datatype':'text',
            'is_multiple': '|', 'name': u'My Tags'}
        }
    mi.set_all_user_metadata(um)
    raw = metadata_to_opf(mi)
    opfc = OPFCreator(os.getcwdu(), other=mi)
    out = StringIO()
    opfc.render(out)
    raw2 = out.getvalue()
    f = StringIO(raw)
    opf = OPF(f)
    f2 = StringIO(raw2)
    opf2 = OPF(f2)
    assert um == opf._user_metadata_
    assert um == opf2._user_metadata_
    print opf.render()

if __name__ == '__main__':
    # test_user_metadata()
    test_m2o()
    test()
