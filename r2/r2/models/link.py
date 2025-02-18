# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of
# the Original Code is reddit Inc.
#
# All portions of the code written by reddit are Copyright (c) 2006-2014 reddit
# Inc. All Rights Reserved.
###############################################################################

from r2.lib.db.thing import (
    Thing, Relation, NotFound, MultiRelation, CreationError)
from r2.lib.db.operators import desc
from r2.lib.utils import (
    base_url,
    domain,
    strip_www,
    timesince,
    title_to_url,
    tup,
    UrlParser,
)
from account import Account, DeletedUser
from subreddit import DefaultSR, DomainSR, Subreddit
from printable import Printable
from r2.config import extensions
from r2.lib.memoize import memoize
from r2.lib.filters import _force_utf8, _force_unicode
from r2.lib import hooks, utils
from r2.lib.log import log_text
from mako.filters import url_escape
from r2.lib.strings import strings, Score
from r2.lib.db import tdb_cassandra
from r2.lib.db.tdb_cassandra import NotFoundException, view_of
from r2.lib.utils import sanitize_url
from r2.models.gold import (
    GildedCommentsByAccount,
    GildedLinksByAccount,
    make_gold_message,
)
from r2.models.subreddit import MultiReddit
from r2.models.query_cache import CachedQueryMutator
from r2.models.promo import PROMOTE_STATUS

from pylons import c, g, request
from pylons.i18n import _
from datetime import datetime, timedelta
from hashlib import md5

import random, re

class LinkExists(Exception): pass

# defining types
class Link(Thing, Printable):
    _data_int_props = Thing._data_int_props + (
        'num_comments', 'reported', 'comment_tree_id', 'gildings')
    _defaults = dict(is_self=False,
                     over_18=False,
                     over_18_override=False,
                     nsfw_str=False,
                     reported=0, num_comments=0,
                     moderator_banned=False,
                     banned_before_moderator=False,
                     media_object=None,
                     secure_media_object=None,
                     promoted=None,
                     pending=False,
                     disable_comments=False,
                     selftext='',
                     sendreplies=True,
                     ip='0.0.0.0',
                     flair_text=None,
                     flair_css_class=None,
                     comment_tree_version=1,
                     comment_tree_id=0,
                     contest_mode=False,
                     skip_commentstree_q="",
                     ignore_reports=False,
                     gildings=0,
                     )
    _essentials = ('sr_id', 'author_id')
    _nsfw = re.compile(r"\bnsfw\b", re.I)

    def __init__(self, *a, **kw):
        Thing.__init__(self, *a, **kw)

    @property
    def has_thumbnail(self):
        return self._t.get('has_thumbnail', hasattr(self, 'thumbnail_url'))

    @classmethod
    def _by_url(cls, url, sr):
        from subreddit import FakeSubreddit
        if isinstance(sr, FakeSubreddit):
            sr = None

        try:
            lbu = LinksByUrl._byID(LinksByUrl._key_from_url(url))
        except tdb_cassandra.NotFound:
            # translate the tdb_cassandra.NotFound into the NotFound
            # the caller is expecting
            raise NotFound('Link "%s"' % url)

        link_id36s = lbu._values()

        links = Link._byID36(link_id36s, data=True, return_dict=False)
        links = [l for l in links if not l._deleted]
        if sr:
            links = [link for link in links if link.sr_id == sr._id]

        if links:
            return links

        raise NotFound('Link "%s"' % url)

    def set_url_cache(self):
        if self.url != 'self':
            LinksByUrl._set_values(LinksByUrl._key_from_url(self.url),
                                   {self._id36: ''})

    @property
    def already_submitted_link(self):
        return self.make_permalink_slow() + '?already_submitted=true'

    def resubmit_link(self, sr_url=False):
        submit_url = self.subreddit_slow.path if sr_url else '/'
        submit_url += 'submit?resubmit=true&url='
        submit_url += url_escape(_force_unicode(self.url))
        return submit_url

    @classmethod
    def _choose_comment_tree_version(cls):
        try:
            weights = g.live_config['comment_tree_version_weights']
        except KeyError:
            return cls._defaults['comment_tree_version']
        try:
            return int(utils.weighted_lottery(weights))
        except ValueError, ex:
            g.log.error("error choosing comment tree version: %s", ex.message)
            return cls._defaults['comment_tree_version']

    @classmethod
    def _submit(cls, title, url, author, sr, ip, spam=False, sendreplies=True):
        from r2.models import admintools

        l = cls(_ups=1,
                title=title,
                url=url,
                _spam=spam,
                author_id=author._id,
                sendreplies=sendreplies,
                sr_id=sr._id,
                lang=sr.lang,
                ip=ip,
                comment_tree_version=cls._choose_comment_tree_version())
        l._commit()
        l.set_url_cache()
        LinksByAccount.add_link(author, l)
        if author._spam:
            g.stats.simple_event('spam.autoremove.link')
            admintools.spam(l, banner='banned user')

        hooks.get_hook('link.new').call(link=l)

        return l

    def _save(self, user, category=None):
        LinkSavesByAccount._save(user, self, category)

    def _unsave(self, user):
        LinkSavesByAccount._unsave(user, self)

    def _hide(self, user):
        LinkHidesByAccount._hide(user, self)

    def _unhide(self, user):
        LinkHidesByAccount._unhide(user, self)

    def link_domain(self):
        if self.is_self:
            return 'self'
        else:
            return domain(self.url)

    def keep_item(self, wrapped):
        user = c.user if c.user_is_loggedin else None

        if not (c.user_is_admin or (isinstance(c.site, DomainSR) and
                                    wrapped.subreddit.is_moderator(user))):
            if self._spam and (not user or
                               (user and self.author_id != user._id)):
                return False

            #author_karma = wrapped.author.link_karma
            #if author_karma <= 0 and random.randint(author_karma, 0) != 0:
                #return False

        if user and not c.ignore_hide_rules:
            if wrapped.hidden:
                return False

            # never automatically hide user's own posts or stickies
            allow_auto_hide = (not wrapped.stickied and
                               self.author_id != user._id)
            if (allow_auto_hide and
                    ((user.pref_hide_ups and wrapped.likes == True) or
                     (user.pref_hide_downs and wrapped.likes == False) or
                     wrapped._score < user.pref_min_link_score)):
                return False

        # Always show NSFW to API users unless obey_over18=true in querystring
        is_api = c.render_style in extensions.API_TYPES
        if is_api and not c.obey_over18:
            return True

        is_nsfw = wrapped.over_18 or wrapped.subreddit.over_18
        return c.over18 or not is_nsfw

    cache_ignore = {
        'subreddit',
        'num_comments',
        'link_child',
        'fresh',
        'media_object',
        'secure_media_object',
    }.union(Printable.cache_ignore)

    @staticmethod
    def wrapped_cache_key(wrapped, style):
        s = Printable.wrapped_cache_key(wrapped, style)
        if wrapped.promoted is not None:
            s.extend([
                getattr(wrapped, "promote_status", -1),
                getattr(wrapped, "disable_comments", False),
                getattr(wrapped, "media_override", False),
                c.user_is_sponsor,
                wrapped.url,
                repr(wrapped.title),
            ])

        if style == "htmllite":
             s.extend([
                 request.GET.has_key('twocolumn'),
                 c.link_target,
            ])
        elif style == "xml":
            s.append(request.GET.has_key("nothumbs"))
        elif style == "compact":
            s.append(c.permalink_page)

        # add link flair to the key if the user and site have enabled it and it
        # exists
        if (c.user.pref_show_link_flair and
                c.site.link_flair_position and
                (wrapped.flair_text or wrapped.flair_css_class)):
            s.append(wrapped.flair_text)
            s.append(wrapped.flair_css_class)
            s.append(c.site.link_flair_position)

        return s

    def make_permalink(self, sr, force_domain=False):
        from r2.lib.template_helpers import get_domain
        p = "comments/%s/%s/" % (self._id36, title_to_url(self.title))
        # promoted links belong to a separate subreddit and shouldn't
        # include that in the path
        if self.promoted is not None:
            if force_domain:
                res = "http://%s/%s" % (get_domain(cname=False,
                                                   subreddit=False), p)
            else:
                res = "/%s" % p
        elif not c.cname and not force_domain:
            res = "/r/%s/%s" % (sr.name, p)
        elif sr != c.site or force_domain:
            if(c.cname and sr == c.site):
                res = "http://%s/%s" % (get_domain(cname=True,
                                                    subreddit=False), p)
            else:
                res = "http://%s/r/%s/%s" % (get_domain(cname=False,
                                                    subreddit=False), sr.name, p)
        else:
            res = "/%s" % p

        # WARNING: If we ever decide to add any ?foo=bar&blah parameters
        # here, Comment.make_permalink will need to be updated or else
        # it will fail.

        return res

    def make_permalink_slow(self, force_domain=False):
        return self.make_permalink(self.subreddit_slow,
                                   force_domain=force_domain)

    def markdown_link_slow(self):
        title = _force_unicode(self.title)
        title = title.replace("[", r"\[")
        title = title.replace("]", r"\]")
        return "[%s](%s)" % (title, self.make_permalink_slow())

    def _gild(self, user):
        now = datetime.now(g.tz)

        self._incr("gildings")
        self.subreddit_slow.add_gilding_seconds()

        GildedLinksByAccount.gild(user, self)

        from r2.lib.db import queries
        with CachedQueryMutator() as m:
            gilding = utils.Storage(thing=self, date=now)
            m.insert(queries.get_all_gilded_links(), [gilding])
            m.insert(queries.get_gilded_links(self.sr_id), [gilding])
            m.insert(queries.get_gilded_user_links(self.author_id),
                     [gilding])
            m.insert(queries.get_user_gildings(user), [gilding])

        hooks.get_hook('link.gild').call(link=self, gilder=user)

    @staticmethod
    def _should_expunge_selftext(link):
        verdict = getattr(link, "verdict", "")
        if verdict not in ("admin-removed", "mod-removed"):
            return False
        if not c.user_is_loggedin:
            return True
        if c.user_is_admin:
            return False
        if c.user == link.author:
            return False
        if link.can_ban:
            return False
        return True

    @classmethod
    def add_props(cls, user, wrapped):
        from r2.lib.pages import make_link_child
        from r2.lib.count import incr_counts
        from r2.lib import media
        from r2.lib.utils import timeago
        from r2.lib.template_helpers import get_domain
        from r2.models.subreddit import FakeSubreddit
        from r2.lib.wrapped import CachedVariable

        # referencing c's getattr is cheap, but not as cheap when it
        # is in a loop that calls it 30 times on 25-200 things.
        user_is_admin = c.user_is_admin
        user_is_loggedin = c.user_is_loggedin
        pref_media = user.pref_media
        pref_frame = user.pref_frame
        cname = c.cname
        site = c.site

        saved = hidden = visited = {}

        if user_is_admin:
            # Checking if a domain's banned isn't even cheap
            urls = [item.url for item in wrapped if hasattr(item, 'url')]
            # bans_for_domain_parts is just a generator; convert to a set for
            # easy use of 'intersection'
            from r2.models.admintools import bans_for_domain_parts
            banned_domains = {ban.domain
                              for ban in bans_for_domain_parts(urls)}

        if user_is_loggedin:
            gilded = [thing for thing in wrapped if thing.gildings > 0]
            try:
                user_gildings = GildedLinksByAccount.fast_query(user, gilded)
            except tdb_cassandra.TRANSIENT_EXCEPTIONS as e:
                g.log.warning("Cassandra gilding lookup failed: %r", e)
                user_gildings = {}

            try:
                saved = LinkSavesByAccount.fast_query(user, wrapped)
                hidden = LinkHidesByAccount.fast_query(user, wrapped)

                if user.gold and user.pref_store_visits:
                    visited = LinkVisitsByAccount.fast_query(user, wrapped)

            except tdb_cassandra.TRANSIENT_EXCEPTIONS as e:
                # saved or hidden or may have been done properly, so go ahead
                # with what we do have
                g.log.warning("Cassandra save/hide/visited lookup failed: %r", e)

        # determine which subreddits the user could assign link flair in
        if user_is_loggedin:
            srs = {item.subreddit for item in wrapped
                                  if item.subreddit.link_flair_position}
            mod_flair_srids = {sr._id for sr in srs
                               if (user_is_admin or
                                   sr.is_moderator_with_perms(c.user, 'flair'))}
            author_flair_srids = {sr._id for sr in srs
                                  if sr.link_flair_self_assign_enabled}

        for item in wrapped:
            show_media = False
            if not hasattr(item, "score_fmt"):
                item.score_fmt = Score.number_only
            if c.render_style == 'compact':
                item.score_fmt = Score.points
            item.pref_compress = user.pref_compress
            if user.pref_compress:
                item.extra_css_class = "compressed"
                item.score_fmt = Score.points
            elif pref_media == 'on' and not user.pref_compress:
                show_media = True
            elif pref_media == 'subreddit' and item.subreddit.show_media:
                show_media = True
            elif item.promoted and item.has_thumbnail:
                if user_is_loggedin and item.author_id == user._id:
                    show_media = True
                elif pref_media != 'off' and not user.pref_compress:
                    show_media = True

            item.nsfw_str = item._nsfw.findall(item.title)
            item.over_18 = bool(item.over_18 or item.subreddit.over_18 or
                                item.nsfw_str)
            item.nsfw = item.over_18 and user.pref_label_nsfw

            item.is_author = (user == item.author)

            item.thumbnail_sprited = False
            # always show a promo author their own thumbnail
            if item.promoted and (user_is_admin or item.is_author) and item.has_thumbnail:
                item.thumbnail = media.thumbnail_url(item)
            elif user.pref_no_profanity and item.over_18 and not c.site.over_18:
                if show_media:
                    item.thumbnail = "nsfw"
                    item.thumbnail_sprited = True
                else:
                    item.thumbnail = ""
            elif not show_media:
                item.thumbnail = ""
            elif (item._deleted or
                  item._spam and item._date < timeago("6 hours")):
                item.thumbnail = "default"
                item.thumbnail_sprited = True
            elif item.has_thumbnail:
                item.thumbnail = media.thumbnail_url(item)
            elif item.is_self:
                item.thumbnail = "self"
                item.thumbnail_sprited = True
            else:
                item.thumbnail = "default"
                item.thumbnail_sprited = True

            item.score = max(0, item.score)

            if getattr(item, "domain_override", None):
                item.domain = item.domain_override
            else:
                item.domain = (domain(item.url) if not item.is_self
                               else 'self.' + item.subreddit.name)

            if user_is_loggedin:
                item.user_gilded = (user, item) in user_gildings
                item.saved = (user, item) in saved
                item.hidden = (user, item) in hidden
                item.visited = (user, item) in visited

            else:
                item.user_gilded = False
                item.saved = item.hidden = item.visited = False

            if c.permalink_page or c.profilepage:
                item.gilded_message = make_gold_message(item, item.user_gilded)
            else:
                item.gilded_message = ''

            item.can_gild = (
                c.user_is_loggedin and
                # you can't gild your own submission
                not (item.author and
                     item.author._id == user._id) and
                # no point in showing the button for things you've already gilded
                not item.user_gilded and
                # ick, if the author deleted their account we shouldn't waste gold
                not item.author._deleted and
                # some subreddits can have gilding disabled
                item.subreddit.allow_gilding
            )

            if item.can_ban:
                # set an attribute on the Wrapped item so that it will be
                # added to the render cache key
                item.ignore_reports_key = item.ignore_reports

            item.num = None
            item.permalink = item.make_permalink(item.subreddit)
            if item.is_self:
                item.url = item.make_permalink(item.subreddit,
                                               force_domain=True)

            if g.shortdomain:
                item.shortlink = g.shortdomain + '/' + item._id36

            item.domain_str = None
            if c.user.pref_domain_details:
                urlparser = UrlParser(item.url)
                if not item.is_self and urlparser.is_reddit_url():
                    url_subreddit = urlparser.get_subreddit()
                    if (url_subreddit and
                            not isinstance(url_subreddit, DefaultSR)):
                        item.domain_str = ('{0}/r/{1}'
                                           .format(item.domain,
                                                   url_subreddit.name))
                elif isinstance(item.media_object, dict):
                    try:
                        author_url = item.media_object['oembed']['author_url']
                        if domain(author_url) == item.domain:
                            urlparser = UrlParser(author_url)
                            item.domain_str = strip_www(urlparser.hostname)
                            item.domain_str += urlparser.path
                    except KeyError:
                        pass

                    if not item.domain_str:
                        try:
                            author = item.media_object['oembed']['author_name']
                            author = _force_unicode(author)
                            item.domain_str = (_force_unicode('{0}: {1}')
                                               .format(item.domain, author))
                        except KeyError:
                            pass

            if not item.domain_str:
                item.domain_str = item.domain

            # do we hide the score?
            if user_is_admin:
                item.hide_score = False
            elif user_is_loggedin and item.subreddit.is_moderator(c.user):
                item.hide_score = False
            elif item.promoted and item.score <= 0:
                item.hide_score = True
            elif user == item.author:
                item.hide_score = False
            elif item._date > timeago("2 hours"):
                item.hide_score = True
            else:
                item.hide_score = False

            # store user preferences locally for caching
            item.pref_frame = pref_frame
            # is this link a member of a different (non-c.site) subreddit?
            item.different_sr = (isinstance(site, FakeSubreddit) or
                                 site.name != item.subreddit.name)
            item.stickied = (not item.different_sr and
                             site.sticky_fullname == item._fullname)

            if user_is_loggedin and item.author_id == user._id:
                item.nofollow = False
            elif item.score <= 1 or item._spam or item.author._spam:
                item.nofollow = True
            else:
                item.nofollow = False

            item.subreddit_path = item.subreddit.path
            if cname:
                item.subreddit_path = ("http://" +
                     get_domain(cname=(site == item.subreddit),
                                subreddit=False))
                if site != item.subreddit:
                    item.subreddit_path += item.subreddit.path
            item.domain_path = "/domain/%s/" % item.domain
            if item.is_self:
                item.domain_path = item.subreddit_path

            # attach video or selftext as needed
            item.link_child, item.editable = make_link_child(item)

            item.tblink = "http://%s/tb/%s" % (
                get_domain(cname=cname, subreddit=False),
                item._id36)

            if item.is_self and not item.promoted:
                item.href_url = item.permalink
            else:
                item.href_url = item.url

            # show the toolbar if the preference is set and the link
            # is neither a promoted link nor a self post
            if pref_frame and not item.is_self and not item.promoted:
                item.mousedown_url = item.tblink
            else:
                item.mousedown_url = None

            item.fresh = not any((item.likes != None,
                                  item.saved,
                                  item.visited,
                                  item.hidden,
                                  item._deleted,
                                  item._spam))

            # bits that we will render stubs (to make the cached
            # version more flexible)
            item.num_text = CachedVariable("num")
            item.commentcls = CachedVariable("commentcls")
            item.comment_label = CachedVariable("numcomments")
            item.lastedited = CachedVariable("lastedited")

            item.as_deleted = False
            if item.deleted and not c.user_is_admin:
                item.author = DeletedUser()
                item.as_deleted = True

            item.votable = item._age < item.subreddit.archive_age

            item.expunged = False
            if item.is_self:
                item.expunged = Link._should_expunge_selftext(item)

            item.editted = getattr(item, "editted", False)

            if user_is_loggedin:
                can_mod_flair = item.subreddit._id in mod_flair_srids
                can_author_flair = (item.is_author and
                                    item.subreddit._id in author_flair_srids)
                item.can_flair = can_mod_flair or can_author_flair
            else:
                item.can_flair = False

            taglinetext = ''
            if item.different_sr:
                author_text = (" <span>" + _("by %(author)s to %(reddit)s") +
                               "</span>")
            else:
                author_text = " <span>" + _("by %(author)s") + "</span>"
            if item.editted:
                if item.score_fmt == Score.points:
                    taglinetext = ("<span>" +
                                   _("%(score)s submitted %(when)s "
                                     "%(lastedited)s") +
                                   "</span>")
                    taglinetext += author_text
                elif item.different_sr:
                    taglinetext = _("submitted %(when)s %(lastedited)s "
                                    "by %(author)s to %(reddit)s")
                else:
                    taglinetext = _("submitted %(when)s %(lastedited)s "
                                    "by %(author)s")
            else:
                if item.score_fmt == Score.points:
                    taglinetext = ("<span>" +
                                   _("%(score)s submitted %(when)s") +
                                   "</span>")
                    taglinetext += author_text
                elif item.different_sr:
                    taglinetext = _("submitted %(when)s by %(author)s "
                                    "to %(reddit)s")
                else:
                    taglinetext = _("submitted %(when)s by %(author)s")
            item.taglinetext = taglinetext

            if item.is_author:
                item.should_incr_counts = False

            if user_is_admin:
                # Link notes
                url = getattr(item, 'url')
                # Pull just the relevant portions out of the url
                urlf = sanitize_url(_force_unicode(url))
                if urlf:
                    urlp = UrlParser(urlf)
                    hostname = urlp.hostname
                    if hostname:
                        parts = (hostname.encode("utf-8").rstrip(".").
                            split("."))
                        subparts = {".".join(parts[y:])
                                    for y in xrange(len(parts))}
                        if subparts.intersection(banned_domains):
                            item.link_notes.append('banned domain')

        if user_is_loggedin:
            incr_counts(wrapped)

        # Run this last
        Printable.add_props(user, wrapped)

    @property
    def subreddit_slow(self):
        """Returns the link's subreddit."""
        # The subreddit is often already on the wrapped link as .subreddit
        # If available, that should be used instead of calling this
        return Subreddit._byID(self.sr_id, data=True, return_dict=False)

    @property
    def author_slow(self):
        """Returns the link's author."""
        # The author is often already on the wrapped link as .author
        # If available, that should be used instead of calling this
        return Account._byID(self.author_id, data=True, return_dict=False)

    def can_flair_slow(self, user):
        """Returns whether the specified user can flair this link"""
        site = self.subreddit_slow
        can_assign_own = (self.author_id == user._id and
                          site.link_flair_self_assign_enabled)

        return site.is_moderator_with_perms(user, 'flair') or can_assign_own

    @classmethod
    def _utf8_encode(cls, value):
        """
        Returns a deep copy of the parameter, UTF-8-encoding any strings
        encountered.
        """
        if isinstance(value, dict):
            return {cls._utf8_encode(key): cls._utf8_encode(value)
                    for key, value in value.iteritems()}
        elif isinstance(value, list):
            return [cls._utf8_encode(item)
                    for item in value]
        elif isinstance(value, unicode):
            return value.encode('utf-8')
        else:
            return value

    # There's an issue where pickling fails for collections with string values
    # that have unicode codepoints between 128 and 256.  Encoding the strings
    # as UTF-8 before storing them works around this.
    def set_media_object(self, value):
        self.media_object = Link._utf8_encode(value)

    def set_secure_media_object(self, value):
        self.secure_media_object = Link._utf8_encode(value)


class LinksByUrl(tdb_cassandra.View):
    _use_db = True
    _connection_pool = 'main'
    _read_consistency_level = tdb_cassandra.CL.ONE

    @classmethod
    def _key_from_url(cls, url):
        if not utils.domain(url) in g.case_sensitive_domains:
            keyurl = _force_utf8(UrlParser.base_url(url.lower()))
        else:
            # Convert only hostname to lowercase
            up = UrlParser(url)
            up.hostname = up.hostname.lower()
            keyurl = _force_utf8(UrlParser.base_url(up.unparse()))
        return keyurl

# Note that there are no instances of PromotedLink or LinkCompressed,
# so overriding their methods here will not change their behaviour
# (except for add_props). These classes are used to override the
# render_class on a Wrapped to change the template used for rendering

class PromotedLink(Link):
    _nodb = True

    # embeds are editable by users (advertisers) so they can change and should
    # be considered in the render cache key
    cache_ignore = Link.cache_ignore - {"media_object", "secure_media_object"}

    @classmethod
    def add_props(cls, user, wrapped):
        Link.add_props(user, wrapped)
        user_is_sponsor = c.user_is_sponsor

        status_dict = dict((v, k) for k, v in PROMOTE_STATUS.iteritems())
        for item in wrapped:
            # these are potentially paid for placement
            item.nofollow = True
            item.user_is_sponsor = user_is_sponsor
            status = getattr(item, "promote_status", -1)
            if item.is_author or c.user_is_sponsor:
                item.rowstyle_cls = "link " + PROMOTE_STATUS.name[status].lower()
            else:
                item.rowstyle_cls = "link promoted"
        # Run this last
        Printable.add_props(user, wrapped)


class Comment(Thing, Printable):
    _data_int_props = Thing._data_int_props + ('reported', 'gildings')
    _defaults = dict(reported=0,
                     parent_id=None,
                     moderator_banned=False,
                     new=False,
                     gildings=0,
                     banned_before_moderator=False,
                     parents=None,
                     ignore_reports=False,
                     )
    _essentials = ('link_id', 'author_id')

    def _markdown(self):
        pass

    @classmethod
    def _new(cls, author, link, parent, body, ip):
        from r2.lib.db.queries import changed

        kw = {}
        if link.comment_tree_version > 1:
            # for top-level comments, parents is an empty string
            # for all others, it looks like "<id36>:<id36>:...".
            if parent:
                if parent.parent_id:
                    if parent.parents is None:
                        parent._fill_in_parents()
                    kw['parents'] = parent.parents + ':' + parent._id36
                else:
                    kw['parents'] = parent._id36

        c = Comment(_ups=1,
                    body=body,
                    link_id=link._id,
                    sr_id=link.sr_id,
                    author_id=author._id,
                    ip=ip,
                    **kw)

        # whitelist promoters commenting on their own promoted links
        from r2.lib import promote
        if promote.is_promo(link) and link.author_id == author._id:
            c._spam = False
        else:
            c._spam = author._spam

        if author._spam:
            g.stats.simple_event('spam.autoremove.comment')

        #these props aren't relations
        if parent:
            c.parent_id = parent._id

        link._incr('num_comments', 1)

        to = None
        name = 'inbox'
        if parent:
            to = Account._byID(parent.author_id, True)
        elif link.sendreplies:
            to = Account._byID(link.author_id, True)
            name = 'selfreply'

        c._commit()

        changed(link, True)  # link's number of comments changed

        CommentsByAccount.add_comment(author, c)

        inbox_rel = None
        # only global admins can be message spammed.
        # Don't send the message if the recipient has blocked
        # the author
        if to and ((not c._spam and author._id not in to.enemies)
            or to.name in g.admins):
            # When replying to your own comment, record the inbox
            # relation, but don't give yourself an orangered
            orangered = (to.name != author.name)
            inbox_rel = Inbox._add(to, c, name, orangered=orangered)

        hooks.get_hook('comment.new').call(comment=c)

        return (c, inbox_rel)

    def _save(self, user, category=None):
        CommentSavesByAccount._save(user, self, category)

    def _unsave(self, user):
        CommentSavesByAccount._unsave(user, self)

    @property
    def subreddit_slow(self):
        from subreddit import Subreddit
        """return's a comments's subreddit. in most case the subreddit is already
        on the wrapped link (as .subreddit), and that should be used
        when possible. if sr_id does not exist, then use the parent link's"""
        self._safe_load()

        if hasattr(self, 'sr_id'):
            sr_id = self.sr_id
        else:
            l = Link._byID(self.link_id, True)
            sr_id = l.sr_id
        return Subreddit._byID(sr_id, True, return_dict=False)

    @property
    def author_slow(self):
        """Returns the comment's author."""
        # The author is often already on the wrapped comment as .author
        # If available, that should be used instead of calling this
        return Account._byID(self.author_id, data=True, return_dict=False)

    def keep_item(self, wrapped):
        return True

    cache_ignore = set(["subreddit", "link", "to", "num_children"]
                       ).union(Printable.cache_ignore)
    @staticmethod
    def wrapped_cache_key(wrapped, style):
        s = Printable.wrapped_cache_key(wrapped, style)
        s.extend([wrapped.body])
        s.extend([hasattr(wrapped, "link") and wrapped.link.contest_mode])
        return s

    def make_permalink(self, link, sr=None, context=None, anchor=False):
        url = link.make_permalink(sr) + self._id36
        if context:
            url += "?context=%d" % context
        if anchor:
            url += "#%s" % self._id36
        return url

    def make_permalink_slow(self, context=None, anchor=False):
        l = Link._byID(self.link_id, data=True)
        return self.make_permalink(l, l.subreddit_slow,
                                   context=context, anchor=anchor)

    def _gild(self, user):
        now = datetime.now(g.tz)

        self._incr("gildings")
        self.subreddit_slow.add_gilding_seconds()

        GildedCommentsByAccount.gild(user, self)

        from r2.lib.db import queries
        with CachedQueryMutator() as m:
            gilding = utils.Storage(thing=self, date=now)
            m.insert(queries.get_all_gilded_comments(), [gilding])
            m.insert(queries.get_gilded_comments(self.sr_id), [gilding])
            m.insert(queries.get_gilded_user_comments(self.author_id),
                     [gilding])
            m.insert(queries.get_user_gildings(user), [gilding])

        hooks.get_hook('comment.gild').call(comment=self, gilder=user)

    def _fill_in_parents(self):
        if not self.parent_id:
            self.parents = ''
            self._commit()
            return
        parent = Comment._byID(self.parent_id)
        if parent.parent_id:
            if parent.parents is None:
                parent._fill_in_parents()
            self.parents = parent.parents + ':' + parent._id36
        else:
            self.parents = parent._id36
        self._commit()

    def parent_path(self):
        """Returns path of comment in tree as list of comment ids.

        The returned list will always begin with -1, followed by comment ids in
        path order. The return value for top-level comments will always be [-1].
        """
        if self.parent_id and self.parents is None:
            self._fill_in_parents()

        if self.parents is None:
            return [-1]

        # eliminate any leading colons from the path and parse
        pids = [long(pid_str, 36) if pid_str else -1
                for pid_str in self.parents.lstrip(':').split(':')]

        # ensure path starts with -1
        if pids[0] != -1:
            pids.insert(0, -1)

        return pids

    @classmethod
    def add_props(cls, user, wrapped):
        from r2.lib.template_helpers import add_attr, get_domain
        from r2.lib.utils import timeago
        from r2.lib.wrapped import CachedVariable
        from r2.lib.pages import WrappedUser

        #fetch parent links
        links = Link._byID(set(l.link_id for l in wrapped), data=True,
                           return_dict=True, stale=True)

        # fetch authors
        authors = Account._byID(set(l.author_id for l in links.values()), data=True,
                                return_dict=True, stale=True)

        #get srs for comments that don't have them (old comments)
        for cm in wrapped:
            if not hasattr(cm, 'sr_id'):
                cm.sr_id = links[cm.link_id].sr_id

        subreddits = Subreddit._byID(set(cm.sr_id for cm in wrapped),
                                     data=True, return_dict=False, stale=True)
        cids = dict((w._id, w) for w in wrapped)
        parent_ids = set(cm.parent_id for cm in wrapped
                         if getattr(cm, 'parent_id', None)
                         and cm.parent_id not in cids)
        parents = {}
        if parent_ids:
            parents = Comment._byID(parent_ids, data=True, stale=True)

        can_reply_srs = set(s._id for s in subreddits if s.can_comment(user)) \
                        if c.user_is_loggedin else set()
        can_reply_srs.add(Subreddit.get_promote_srid())

        min_score = user.pref_min_comment_score

        profilepage = c.profilepage
        user_is_admin = c.user_is_admin
        user_is_loggedin = c.user_is_loggedin
        focal_comment = c.focal_comment
        cname = c.cname
        site = c.site

        if user_is_loggedin:
            gilded = [thing for thing in wrapped if thing.gildings > 0]
            try:
                user_gildings = GildedCommentsByAccount.fast_query(user, gilded)
            except tdb_cassandra.TRANSIENT_EXCEPTIONS as e:
                g.log.warning("Cassandra gilding lookup failed: %r", e)
                user_gildings = {}

            try:
                saved = CommentSavesByAccount.fast_query(user, wrapped)
            except tdb_cassandra.TRANSIENT_EXCEPTIONS as e:
                g.log.warning("Cassandra comment save lookup failed: %r", e)
                saved = {}
        else:
            user_gildings = {}
            saved = {}

        for item in wrapped:
            # for caching:
            item.profilepage = c.profilepage
            item.link = links.get(item.link_id)

            if (item.link._score <= 1 or item.score < 3 or
                item.link._spam or item._spam or item.author._spam):
                item.nofollow = True
            else:
                item.nofollow = False

            if not hasattr(item, 'subreddit'):
                item.subreddit = item.subreddit_slow
            if item.author_id == item.link.author_id and not item.link._deleted:
                add_attr(item.attribs, 'S',
                         link=item.link.make_permalink(item.subreddit))
            if not hasattr(item, 'target'):
                item.target = "_top" if cname else None
            if item.parent_id:
                if item.parent_id in parents:
                    parent = parents[item.parent_id]
                else:
                    parent = cids[item.parent_id]
                if not parent.deleted:
                    if item.parent_id in cids:
                        item.parent_permalink = '#' + utils.to36(item.parent_id)
                    else:
                        item.parent_permalink = parent.make_permalink(item.link, item.subreddit)
                else:
                    item.parent_permalink = None
            else:
                item.parent_permalink = None

            item.can_reply = False
            if c.can_reply or (item.sr_id in can_reply_srs):
                if item.link._age < item.subreddit.archive_age:
                    item.can_reply = True

            item.can_save = c.can_save or False

            if user_is_loggedin:
                item.user_gilded = (user, item) in user_gildings
                item.saved = (user, item) in saved
                item.can_save = True
            else:
                item.user_gilded = False
                item.saved = False
            item.gilded_message = make_gold_message(item, item.user_gilded)

            item.can_gild = (
                # this is a way of checking if the user is logged in that works
                # both within CommentPane instances and without.  e.g. CommentPane
                # explicitly sets user_is_loggedin = False but can_reply is
                # correct.  while on user overviews, you can't reply but will get
                # the correct value for user_is_loggedin
                (c.user_is_loggedin or getattr(item, "can_reply", True)) and
                # you can't gild your own comment
                not (c.user_is_loggedin and
                     item.author and
                     item.author._id == user._id) and
                # no point in showing the button for things you've already gilded
                not item.user_gilded and
                # ick, if the author deleted their account we shouldn't waste gold
                not item.author._deleted and
                # some subreddits can have gilding disabled
                item.subreddit.allow_gilding
            )

            # not deleted on profile pages,
            # deleted if spam and not author or admin
            item.deleted = (not profilepage and
                           (item._deleted or
                            (item._spam and
                             item.author != user and
                             not item.show_spam)))

            item.have_form = not item.deleted

            extra_css = ''
            if item.deleted:
                extra_css += "grayed"
                if not user_is_admin:
                    item.author = DeletedUser()
                    item.body = '[deleted]'
                    item.gildings = 0
                    item.distinguished = None

            if focal_comment == item._id36:
                extra_css += " border"

            if profilepage:
                link_author = authors[item.link.author_id]
                if ((item.link._deleted or link_author._deleted) and
                        not user_is_admin):
                    link_author = DeletedUser()
                item.link_author = WrappedUser(link_author)
                item.full_comment_path = item.link.make_permalink(item.subreddit)
                item.full_comment_count = item.link.num_comments
            else:
                # these aren't used so set them to constant values to avoid
                # invalidating items in render cache
                item.full_comment_path = ''
                item.full_comment_count = 0

            item.subreddit_path = item.subreddit.path
            if cname:
                item.subreddit_path = ("http://" +
                     get_domain(cname=(site == item.subreddit),
                                subreddit=False))
                if site != item.subreddit:
                    item.subreddit_path += item.subreddit.path

            # don't collapse for admins, on profile pages, or if deleted
            item.collapsed = False
            if ((item.score < min_score) and not (profilepage or
                item.deleted or user_is_admin)):
                item.collapsed = True
                item.collapsed_reason = _("comment score below threshold")
            if user_is_loggedin and item.author_id in c.user.enemies:
                if "grayed" not in extra_css:
                    extra_css += " grayed"
                item.collapsed = True
                item.collapsed_reason = _("blocked user")

            item.editted = getattr(item, "editted", False)

            item.render_css_class = "comment %s" % CachedVariable("time_period")

            #will get updated in builder
            item.num_children = 0
            item.numchildren_text = CachedVariable("numchildren_text")
            item.score_fmt = Score.points
            item.permalink = item.make_permalink(item.link, item.subreddit)

            item.is_author = (user == item.author)
            item.is_focal = (focal_comment == item._id36)

            item.votable = item._age < item.subreddit.archive_age

            hide_period = ('{0} minutes'
                          .format(item.subreddit.comment_score_hide_mins))

            if item.link.contest_mode:
                item.score_hidden = True
            elif item._date > timeago(hide_period):
                item.score_hidden = not item.is_author
            else:
                item.score_hidden = False

            if item.score_hidden and c.user_is_loggedin:
                if c.user_is_admin or item.subreddit.is_moderator(c.user):
                    item.score_hidden = False

            if item.score_hidden:
                item.upvotes = 1
                item.downvotes = 0
                item.score = 1
                item.voting_score = [1, 1, 1]
                item.render_css_class += " score-hidden"

            # in contest mode, use only upvotes for the score if the subreddit
            # has been (manually) set to do so
            if (item.link.contest_mode and
                    item.subreddit.contest_mode_upvotes_only and
                    not item.score_hidden):
                item.score = item._ups
                item.voting_score = [
                    item.score - 1, item.score, item.score + 1]
                item.collapsed = False

            #will seem less horrible when add_props is in pages.py
            from r2.lib.pages import UserText
            item.usertext = UserText(item, item.body,
                                     editable=item.is_author,
                                     nofollow=item.nofollow,
                                     target=item.target,
                                     extra_css=extra_css,
                                     have_form=item.have_form)

            item.lastedited = CachedVariable("lastedited")

        # Run this last
        Printable.add_props(user, wrapped)

class CommentSortsCache(tdb_cassandra.View):
    """A cache of the sort-values of comments to avoid looking up all
       of the comments in a big tree at render-time just to determine
       the candidate order"""
    _use_db = True
    _value_type = 'float'
    _connection_pool = 'main'
    _read_consistency_level = tdb_cassandra.CL.ONE
    _fetch_all_columns = True

class StarkComment(Comment):
    """Render class for the comments in the top-comments display in
       the reddit toolbar"""
    _nodb = True

class MoreMessages(Printable):
    cachable = False
    display = ""
    new = False
    was_comment = False
    is_collapsed = True

    def __init__(self, parent, child):
        self.parent = parent
        self.child = child

    @staticmethod
    def wrapped_cache_key(item, style):
        return False

    @property
    def _fullname(self):
        return self.parent._fullname

    @property
    def _id36(self):
        return self.parent._id36

    @property
    def subject(self):
        return self.parent.subject

    @property
    def childlisting(self):
        return self.child

    @property
    def to(self):
        return self.parent.to

    @property
    def author(self):
        return self.parent.author

    @property
    def recipient(self):
        return self.parent.recipient

    @property
    def sr_id(self):
        return self.parent.sr_id

    @property
    def subreddit(self):
        return self.parent.subreddit


class MoreComments(Printable):
    cachable = False
    display = ""

    @staticmethod
    def wrapped_cache_key(item, style):
        return False

    def __init__(self, link, depth, parent_id=None):
        from r2.lib.wrapped import CachedVariable

        if parent_id is not None:
            id36 = utils.to36(parent_id)
            self.parent_id = parent_id
            self.parent_name = "t%s_%s" % (utils.to36(Comment._type_id), id36)
            self.parent_permalink = link.make_permalink_slow() + id36
        self.link_name = link._fullname
        self.link_id = link._id
        self.depth = depth
        self.children = []
        self.count = 0
        self.previous_visits_hex = CachedVariable("previous_visits_hex")

    @property
    def _fullname(self):
        return "t%s_%s" % (utils.to36(Comment._type_id), self._id36)

    @property
    def _id36(self):
        return utils.to36(self.children[0]) if self.children else '_'


class MoreRecursion(MoreComments):
    pass

class MoreChildren(MoreComments):
    pass

class Message(Thing, Printable):
    _defaults = dict(reported=0,
                     was_comment=False,
                     parent_id=None,
                     new=False,
                     first_message=None,
                     to_id=None,
                     sr_id=None,
                     to_collapse=None,
                     author_collapse=None,
                     from_sr=False)
    _data_int_props = Thing._data_int_props + ('reported',)
    _essentials = ('author_id',)
    cache_ignore = set(["to", "subreddit"]).union(Printable.cache_ignore)

    @classmethod
    def _new(cls, author, to, subject, body, ip, parent=None, sr=None,
             from_sr=False):
        m = Message(subject=subject, body=body, author_id=author._id, new=True,
                    ip=ip, from_sr=from_sr)
        m._spam = author._spam

        if author._spam:
            g.stats.simple_event('spam.autoremove.message')

        sr_id = None
        # check to see if the recipient is a subreddit and swap args accordingly
        if to and isinstance(to, Subreddit):
            if from_sr:
                raise CreationError("Cannot send from SR to SR")
            to_subreddit = True
            to, sr = None, to
        else:
            to_subreddit = False

        if sr:
            sr_id = sr._id
        if parent:
            m.parent_id = parent._id
            if parent.first_message:
                m.first_message = parent.first_message
            else:
                m.first_message = parent._id
            if parent.sr_id:
                sr_id = parent.sr_id

        if not to and not sr_id:
            raise CreationError("Message created with neither to nor sr_id")
        if from_sr and not sr_id:
            raise CreationError("Message sent from_sr without setting sr")

        m.to_id = to._id if to else None
        if sr_id is not None:
            m.sr_id = sr_id

        m._commit()

        MessagesByAccount.add_message(author, m)

        if sr_id and not sr:
            sr = Subreddit._byID(sr_id)

        inbox_rel = []

        inbox_hook = hooks.get_hook("message.skip_inbox")
        skip_inbox = inbox_hook.call_until_return(message=m)
        if skip_inbox:
            m._spam = True
            m._commit()

        if not skip_inbox and sr_id:
            # if there is a subreddit id, and it's either a reply or
            # an initial message to an SR, add to the moderator inbox
            # (i.e., don't do it for automated messages from the SR)
            if parent or to_subreddit and not from_sr:
                inbox_rel.append(ModeratorInbox._add(sr, m, 'inbox'))
            if sr.is_moderator(author):
                m.distinguished = 'yes'
                m._commit()

        if author.name in g.admins:
            m.distinguished = 'admin'
            m._commit()

        # if there is a "to" we may have to create an inbox relation as well
        # also, only global admins can be message spammed.
        if not skip_inbox and to and (not m._spam or to.name in g.admins):
            # if the current "to" is not a sr moderator,
            # they need to be notified
            if not sr_id or not sr.is_moderator(to):
                # Record the inbox relation, but don't give the user
                # an orangered, if they PM themselves.
                # Don't notify on PMs from blocked users, either
                orangered = (to.name != author.name and
                             author._id not in to.enemies)
                inbox_rel.append(Inbox._add(to, m, 'inbox',
                                            orangered=orangered))
            # find the message originator
            elif sr_id and m.first_message:
                first = Message._byID(m.first_message, True)
                orig = Account._byID(first.author_id, True)
                # Only inbox if the origin account does not have
                # modmail access.
                if (orig._id != author._id and
                        not sr.is_moderator_with_perms(orig, 'mail')):
                    inbox_rel.append(Inbox._add(orig, m, 'inbox'))

        hooks.get_hook('message.new').call(message=m)

        return (m, inbox_rel)

    @property
    def permalink(self):
        return "/message/messages/%s" % self._id36

    def can_view_slow(self):
        if c.user_is_loggedin:
            # simple case from before:
            if (c.user_is_admin or
                c.user._id in (self.author_id, self.to_id)):
                return True
            elif self.sr_id:
                sr = Subreddit._byID(self.sr_id)
                is_moderator = sr.is_moderator_with_perms(c.user, 'mail')
                # moderators can view messages on subreddits they moderate
                if is_moderator:
                    return True
                elif self.first_message:
                    first = Message._byID(self.first_message, True)
                    return (first.author_id == c.user._id)


    @classmethod
    def add_props(cls, user, wrapped):
        from r2.lib.db import queries
        #TODO global-ish functions that shouldn't be here?
        #reset msgtime after this request
        msgtime = c.have_messages

        # make sure there is a sr_id set:
        for w in wrapped:
            if not hasattr(w, "sr_id"):
                w.sr_id = None

        # load the to fields if one exists
        to_ids = set(w.to_id for w in wrapped if w.to_id is not None)
        tos = Account._byID(to_ids, True) if to_ids else {}

        # load the subreddit field if one exists:
        sr_ids = set(w.sr_id for w in wrapped if w.sr_id is not None)
        m_subreddits = Subreddit._byID(sr_ids, data=True, return_dict=True)

        # load the links and their subreddits (if comment-as-message)
        links = Link._byID(set(l.link_id for l in wrapped if l.was_comment),
                           data=True,
                           return_dict=True)
        # subreddits of the links (for comment-as-message)
        l_subreddits = Subreddit._byID(set(l.sr_id for l in links.values()),
                                       data=True, return_dict=True)

        parents = Comment._byID(set(l.parent_id for l in wrapped
                                  if l.parent_id and l.was_comment),
                                data=True, return_dict=True)

        # load the unread list to determine message newness
        unread = set(queries.get_unread_inbox(user))

        msg_srs = set(m_subreddits[x.sr_id]
                      for x in wrapped if x.sr_id is not None
                      and isinstance(x.lookups[0], Message))
        # load the unread mod list for the same reason
        mod_unread = set(queries.get_unread_subreddit_messages_multi(msg_srs))

        for item in wrapped:
            item.to = tos.get(item.to_id)
            if item.sr_id:
                item.recipient = (item.author_id != c.user._id)
            else:
                item.recipient = (item.to_id == c.user._id)

            if item.author_id == c.user._id:
                item.new = False
            elif item._fullname in unread:
                item.new = True
                # wipe new messages if preferences say so, and this isn't a feed
                # and it is in the user's personal inbox
                if (item.new and c.user.pref_mark_messages_read
                    and c.extension not in ("rss", "xml", "api", "json")):
                    queries.set_unread(item.lookups[0],
                                       c.user, False)
            else:
                item.new = item._fullname in mod_unread

            item.score_fmt = Score.none

            item.message_style = ""
            # comment as message:
            if item.was_comment:
                link = links[item.link_id]
                sr = l_subreddits[link.sr_id]
                item.to_collapse = False
                item.author_collapse = False
                item.link_title = link.title
                item.permalink = item.lookups[0].make_permalink(link, sr=sr)
                item.link_permalink = link.make_permalink(sr)
                if item.parent_id:
                    parent = parents[item.parent_id]
                    item.parent = parent._fullname
                    item.parent_permalink = parent.make_permalink(link, sr)

                    if parent.author_id == c.user._id:
                        item.subject = _('comment reply')
                        item.message_style = "comment-reply"
                    else:
                        item.subject = _('username mention')
                        item.message_style = "mention"
                else:
                    if link.author_id == c.user._id:
                        item.subject = _('post reply')
                        item.message_style = "post-reply"
                    else:
                        item.subject = _('username mention')
                        item.message_style = "mention"
            elif item.sr_id is not None:
                item.subreddit = m_subreddits[item.sr_id]

            item.hide_author = False
            if getattr(item, "from_sr", False):
                if not (item.subreddit.is_moderator(c.user) or
                        c.user_is_admin):
                    item.author = item.subreddit
                    item.hide_author = True

            item.is_collapsed = None
            if not item.new:
                if item.recipient:
                    item.is_collapsed = item.to_collapse
                if item.author_id == c.user._id:
                    item.is_collapsed = item.author_collapse
                if c.user.pref_collapse_read_messages:
                    item.is_collapsed = (item.is_collapsed is not False)
            if item.author_id in c.user.enemies and not item.was_comment:
                item.is_collapsed = True
                if not c.user_is_admin:
                    item.subject = _('[message from blocked user]')
                    item.body = _('[unblock user to see this message]')
            taglinetext = ''
            if item.hide_author:
                taglinetext = _("subreddit message %(author)s sent %(when)s")
            elif item.author_id == c.user._id:
                taglinetext = _("to %(dest)s sent %(when)s")
            elif item.to_id == c.user._id or item.to_id is None:
                taglinetext = _("from %(author)s sent %(when)s")
            else:
                taglinetext = _("to %(dest)s from %(author)s sent %(when)s")
            item.taglinetext = taglinetext
            if item.to:
                if item.to._deleted:
                    item.dest = "[deleted]"
                else:
                    item.dest = item.to.name
            else:
                item.dest = ""
            if item.sr_id:
                if item.hide_author:
                    item.updated_author = _("via %(subreddit)s")
                else:
                    item.updated_author = _("%(author)s via %(subreddit)s")
            else:
                item.updated_author = ''


        # Run this last
        Printable.add_props(user, wrapped)

    @property
    def subreddit_slow(self):
        from subreddit import Subreddit
        if self.sr_id:
            return Subreddit._byID(self.sr_id)

    @property
    def author_slow(self):
        """Returns the message's author."""
        # The author is often already on the wrapped message as .author
        # If available, that should be used instead of calling this
        return Account._byID(self.author_id, data=True, return_dict=False)

    @staticmethod
    def wrapped_cache_key(wrapped, style):
        s = Printable.wrapped_cache_key(wrapped, style)
        s.extend([wrapped.new, wrapped.collapsed])
        return s

    def keep_item(self, wrapped):
        return True


class _SaveHideByAccount(tdb_cassandra.DenormalizedRelation):
    @classmethod
    def value_for(cls, thing1, thing2):
        return ''

    @classmethod
    def _cached_queries(cls, user, thing):
        return []

    @classmethod
    def _savehide(cls, user, things, **kw):
        things = tup(things)
        now = datetime.now(g.tz)
        with CachedQueryMutator() as m:
            for thing in things:
                # action_date is only used by the cached queries as the sort
                # value, we don't want to write it. Report.new(link) needs to
                # incr link.reported but will fail if the link is dirty.
                thing.__setattr__('action_date', now, make_dirty=False)
                for q in cls._cached_queries(user, thing, **kw):
                    m.insert(q, [thing])
        cls.create(user, things, **kw)

    @classmethod
    def destroy(cls, user, things, **kw):
        things = tup(things)
        cls._cf.remove(user._id36, (things._id36 for things in things))

        for view in cls._views:
            view.destroy(user, things, **kw)

    @classmethod
    def _unsavehide(cls, user, things, **kw):
        things = tup(things)
        with CachedQueryMutator() as m:
            for thing in things:
                for q in cls._cached_queries(user, thing, **kw):
                    m.delete(q, [thing])
        cls.destroy(user, things, **kw)


class _ThingSavesByAccount(_SaveHideByAccount):
    _read_consistency_level = tdb_cassandra.CL.QUORUM
    _write_consistency_level = tdb_cassandra.CL.QUORUM

    @classmethod
    def value_for(cls, thing1, thing2, category=None):
        return category or ''
    
    @classmethod
    def _remove_from_category_listings(cls, user, things, category):
        things = tup(things)
        oldcategories = cls.fast_query(user, things)
        changedthings = []
        for thing in things:
            oldcategory = oldcategories.get((user, thing)) or None
            if oldcategory != category:
                changedthings.append(thing)
        cls._unsavehide(user, changedthings, categories=oldcategories)

    @classmethod
    def _save(cls, user, things, category=None):
        category = category.lower() if category else None
        cls._remove_from_category_listings(user, things, category=category)
        cls._savehide(user, things, category=category)

    @classmethod
    def _unsave(cls, user, things):
        # Ensure we delete from existing category cached queries
        categories = cls.fast_query(user, tup(things))
        cls._unsavehide(user, things, categories=categories)

    @classmethod
    def _unsavehide(cls, user, things, categories=None):
        things = tup(things)
        with CachedQueryMutator() as m:
            for thing in things:
                category = categories.get((user, thing)) if categories else None
                for q in cls._cached_queries(user, thing, category=category):
                    m.delete(q, [thing])
        cls.destroy(user, things, categories=categories)

    @classmethod
    def _cached_queries_category(cls, user, thing,
                                 querycatfn, queryfn,
                                 category=None, only_category=False):
        from r2.lib.db import queries
        cached_queries = []
        if not only_category:
            cached_queries = [queryfn(user, 'none'), queryfn(user, thing.sr_id)]
        if category:
            cached_queries.append(querycatfn(user, 'none', category))
            cached_queries.append(querycatfn(user, thing.sr_id, category))
        return cached_queries

class LinkSavesByAccount(_ThingSavesByAccount):
    _use_db = True
    _last_modified_name = 'Save'
    _views = []

    @classmethod
    def _cached_queries(cls, user, thing, **kw):
        from r2.lib.db import queries
        return cls._cached_queries_category(
            user,
            thing,
            queries.get_categorized_saved_links,
            queries.get_saved_links,
            **kw)

class CommentSavesByAccount(_ThingSavesByAccount):
    _use_db = True
    _last_modified_name = 'CommentSave'
    _views = []

    @classmethod
    def _cached_queries(cls, user, thing, **kw):
        from r2.lib.db import queries
        return cls._cached_queries_category(
            user,
            thing,
            queries.get_categorized_saved_comments,
            queries.get_saved_comments,
            **kw)

class _ThingHidesByAccount(_SaveHideByAccount):
    @classmethod
    def _hide(cls, user, things):
        cls._savehide(user, things)

    @classmethod
    def _unhide(cls, user, things):
        cls._unsavehide(user, things)


class LinkHidesByAccount(_ThingHidesByAccount):
    _use_db = True
    _last_modified_name = 'Hide'
    _views = []

    @classmethod
    def _cached_queries(cls, user, thing):
        from r2.lib.db import queries
        return [queries.get_hidden_links(user)]

class LinkVisitsByAccount(_SaveHideByAccount):
    _use_db = True
    _last_modified_name = 'Visit'
    _views = []
    _ttl = timedelta(days=7)
    _write_consistency_level = tdb_cassandra.CL.ONE

    @classmethod
    def _visit(cls, user, things):
        cls._savehide(user, things)

    @classmethod
    def _unvisit(cls, user, things):
        cls._unsavehide(user, things)

class _ThingSavesBySubreddit(tdb_cassandra.View):
    @classmethod
    def _rowkey(cls, user, thing):
        return user._id36

    @classmethod
    def _column(cls, user, thing):
        return {utils.to36(thing.sr_id): ''}

    @classmethod
    def get_saved_values(cls, user):
        rowkey = cls._rowkey(user, None)
        try:
            columns = cls._cf.get(rowkey,
                                  column_count=tdb_cassandra.max_column_count)
        except NotFoundException:
            return []

        return columns.keys()

    @classmethod
    def get_saved_subreddits(cls, user):
        sr_id36s = cls.get_saved_values(user)
        srs = Subreddit._byID36(sr_id36s, return_dict=False, data=True)
        return sorted([sr.name for sr in srs])

    @classmethod
    def create(cls, user, things, **kw):
        for thing in things:
            rowkey = cls._rowkey(user, thing)
            column = cls._column(user, thing)
            cls._set_values(rowkey, column)

    @classmethod
    def _check_empty(cls, user, sr_id):
        return False

    @classmethod
    def destroy(cls, user, things, **kw):
        # See if thing's sr is present anymore
        sr_ids = set([thing.sr_id for thing in things])
        for sr_id in set(sr_ids):
            if cls._check_empty(user, sr_id):
                cls._cf.remove(user._id36, [utils.to36(sr_id)])

class _ThingSavesByCategory(_ThingSavesBySubreddit):
    @classmethod
    def create(cls, user, things, category=None):
        if not category:
            return
        for thing in things:
            rowkey = cls._rowkey(user, thing)
            column = {category: None}
            cls._set_values(rowkey, column)

    @classmethod
    def _get_query_fn():
        raise NotImplementedError 

    @classmethod
    def _check_empty(cls, user, category):
        from r2.lib.db import queries
        q = cls._get_query_fn()(user, 'none', category)
        q.fetch()
        return not q.data

    @classmethod
    def get_saved_categories(cls, user):
        return cls.get_saved_values(user)

    @classmethod
    def destroy(cls, user, things, categories=None):
        if not categories:
            return
        for category in set(categories.values()):
            if not category or not cls._check_empty(user, category):
                continue
            cls._cf.remove(user._id36, [category])

@view_of(LinkSavesByAccount)
class LinkSavesByCategory(_ThingSavesByCategory):
    _use_db = True

    @classmethod
    def _get_query_fn(cls):
        from r2.lib.db import queries
        return queries.get_categorized_saved_links

@view_of(LinkSavesByAccount)
class LinkSavesBySubreddit(_ThingSavesBySubreddit):
    _use_db = True

    @classmethod
    def _check_empty(cls, user, sr_id):
        from r2.lib.db import queries
        q = queries.get_saved_links(user, sr_id)
        q.fetch()
        return not q.data


@view_of(CommentSavesByAccount)
class CommentSavesBySubreddit(_ThingSavesBySubreddit):
    _use_db = True

    @classmethod
    def _check_empty(cls, user, sr_id):
        from r2.lib.db import queries
        q = queries.get_saved_comments(user, sr_id)
        q.fetch()
        return not q.data

@view_of(CommentSavesByAccount)
class CommentSavesByCategory(_ThingSavesByCategory):
    _use_db = True

    @classmethod
    def _get_query_fn(cls):
        from r2.lib.db import queries
        return queries.get_categorized_saved_comments

class Inbox(MultiRelation('inbox',
                          Relation(Account, Comment),
                          Relation(Account, Message))):
    @classmethod
    def _add(cls, to, obj, *a, **kw):
        orangered = kw.pop("orangered", True)
        i = Inbox(to, obj, *a, **kw)
        i.new = True
        i._commit()

        if not to._loaded:
            to._load()

        #if there is not msgtime, or it's false, set it
        if orangered and (not hasattr(to, 'msgtime') or not to.msgtime):
            to.msgtime = obj._date
            to._commit()

        return i

    @classmethod
    def set_unread(cls, things, unread, to=None):
        things = tup(things)
        if len(set(type(x) for x in things)) != 1:
            raise TypeError('things must only be of a single type')
        thing_ids = [x._id for x in things]
        inbox_rel = cls.rel(Account, things[0].__class__)
        if to:
            inbox = inbox_rel._query(inbox_rel.c._thing2_id == thing_ids,
                                     inbox_rel.c._thing1_id == to._id,
                                     data=True)
        else:
            inbox = inbox_rel._query(inbox_rel.c._thing2_id == thing_ids,
                                     data=True)
        res = []
        for i in inbox:
            if getattr(i, "new", False) != unread:
                i.new = unread
                i._commit()
            res.append(i)
        return res


class ModeratorInbox(Relation(Subreddit, Message)):
    #TODO: shouldn't dupe this
    @classmethod
    def _add(cls, sr, obj, *a, **kw):
        i = ModeratorInbox(sr, obj, *a, **kw)
        i.new = True
        i._commit()

        if not sr._loaded:
            sr._load()

        mod_perms = sr.moderators_with_perms()
        mod_ids = set(mod_id for mod_id, perms in mod_perms.iteritems()
                      if perms.get('mail', False))
        moderators = Account._byID(mod_ids, data=True, return_dict=False)
        for m in moderators:
            if obj.author_id != m._id and not m.modmsgtime:
                m.modmsgtime = obj._date
                m._commit()

        return i

    @classmethod
    def set_unread(cls, things, unread):
        things = tup(things)
        thing_ids = [x._id for x in things]
        inbox = cls._query(cls.c._thing2_id == thing_ids, data=True)
        res = []
        for i in inbox:
            if getattr(i, "new", False) != unread:
                i.new = unread
                i._commit()
            res.append(i)
        return res

class CommentsByAccount(tdb_cassandra.DenormalizedRelation):
    _use_db = True
    _write_last_modified = False
    _views = []

    @classmethod
    def value_for(cls, thing1, thing2):
        return ''

    @classmethod
    def add_comment(cls, account, comment):
        cls.create(account, [comment])


class LinksByAccount(tdb_cassandra.DenormalizedRelation):
    _use_db = True
    _write_last_modified = False
    _views = []

    @classmethod
    def value_for(cls, thing1, thing2):
        return ''

    @classmethod
    def add_link(cls, account, link):
        cls.create(account, [link])


class MessagesByAccount(tdb_cassandra.DenormalizedRelation):
    _use_db = True
    _write_last_modified = False
    _views = []

    @classmethod
    def value_for(cls, thing1, thing2):
        return ''

    @classmethod
    def add_message(cls, account, message):
        cls.create(account, [message])
