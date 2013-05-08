'''A pile contains subpiles which contain tracesfiles which contain traces.'''

import trace, io, util, config

import numpy as num
import os, pickle, logging, time, weakref, copy, re, sys
import cPickle as pickle
pjoin = os.path.join
logger = logging.getLogger('pyrocko.pile')

from util import reuse
from trace import degapper


progressbar = util.progressbar_module()

class TracesFileCache(object):
    '''Manages trace metainformation cache.
    
    For each directory with files containing traces, one cache file is 
    maintained to hold the trace metainformation of all files which are 
    contained in the directory.
    '''

    caches = {}

    def __init__(self, cachedir):
        '''Create new cache.
        
        :param cachedir: directory to hold the cache files.
          
        '''
        
        self.cachedir = cachedir
        self.dircaches = {}
        self.modified = set()
        util.ensuredir(self.cachedir)
        
    def get(self, abspath):
        '''Try to get an item from the cache.
        
        :param abspath: absolute path of the object to retrieve
          
        :returns: a stored object is returned or None if nothing could be found.
          
        '''
        
        dircache = self._get_dircache_for(abspath)
        if abspath in dircache:
            return dircache[abspath]
        return None

    def put(self, abspath, tfile):
        '''Put an item into the cache.
        
        :param abspath: absolute path of the object to be stored
        :param tfile: object to be stored
        '''
        
        cachepath = self._dircachepath(abspath)
        # get lock on cachepath here
        dircache = self._get_dircache(cachepath)
        dircache[abspath] = tfile
        self.modified.add(cachepath)

    def dump_modified(self):
        '''Save any modifications to disk.'''

        for cachepath in self.modified:
            self._dump_dircache(self.dircaches[cachepath], cachepath)
            # unlock 
            
        self.modified = set()

    def clean(self):
        '''Weed out missing files from the disk caches.'''
        
        self.dump_modified()
        
        for fn in os.listdir(self.cachedir):
            try:
                i = int(fn) # valid filenames are integers
                cache = self._load_dircache(pjoin(self.cachedir, fn))
                self._dump_dircache(cache, pjoin(self.cachedir, fn))
                
            except ValueError:
                pass

    def _get_dircache_for(self, abspath):
        return self._get_dircache(self._dircachepath(abspath))
    
    def _get_dircache(self, cachepath):
        if cachepath not in self.dircaches:
            if os.path.isfile(cachepath):
                self.dircaches[cachepath] = self._load_dircache(cachepath)
            else:
                self.dircaches[cachepath] = {}
                
        return self.dircaches[cachepath]
       
    def _dircachepath(self, abspath):
        cachefn = "%i" % abs(hash(os.path.dirname(abspath)))
        return  pjoin(self.cachedir, cachefn)
            
    def _load_dircache(self, cachefilename):
        
        f = open(cachefilename,'r')
        cache = pickle.load(f)
        f.close()
        
        # weed out files which no longer exist
        for fn in cache.keys():
            if not os.path.isfile(fn):
                del cache[fn]
        return cache
        
    def _dump_dircache(self, cache, cachefilename):
        
        if not cache:
            if os.path.exists(cachefilename):
                os.remove(cachefilename)
            return
        
        # make a copy without the parents
        cache_copy = {}
        for fn in cache.keys():
            cache_copy[fn] = copy.copy(cache[fn])
            cache_copy[fn].parent = None
        
        tmpfn = cachefilename+'.%i.tmp' % os.getpid()
        f = open(tmpfn, 'w')
        pickle.dump(cache_copy, f)
        f.close()
        os.rename(tmpfn, cachefilename)


def get_cache(cachedir):
    '''Get global TracesFileCache object for given directory.'''
    if cachedir not in TracesFileCache.caches:
        TracesFileCache.caches[cachedir] = TracesFileCache(cachedir)
        
    return TracesFileCache.caches[cachedir]
    
def loader(filenames, fileformat, cache, filename_attributes, show_progress=True):
        
    if not filenames:
        logger.warn('No files to load from')
        return
    
    pbar = None
    if show_progress and progressbar and config.show_progress:
        widgets = ['Scanning files', ' ',
                progressbar.Bar(marker='-',left='[',right=']'), ' ',
                progressbar.Percentage(), ' ',]
        
        pbar = progressbar.ProgressBar(widgets=widgets, maxval=len(filenames)).start()
    
    regex = None
    if filename_attributes:
        regex = re.compile(filename_attributes)
    
    failures = []
    for ifile, filename in enumerate(filenames):
        try:
            abspath = os.path.abspath(filename)
            
            substitutions = None
            if regex:
                m = regex.search(filename)
                if not m: raise FilenameAttributeError(
                    "Cannot get attributes with pattern '%s' from path '%s'" 
                        % (filename_attributes, filename))
                substitutions = {}
                for k in m.groupdict():
                    if k  in ('network', 'station', 'location', 'channel'):
                        substitutions[k] = m.groupdict()[k]
                
            
            mtime = os.stat(filename)[8]
            tfile = None
            if cache:
                tfile = cache.get(abspath)
            
            if not tfile or tfile.mtime != mtime or substitutions:
                tfile = TracesFile(None, abspath, fileformat, substitutions=substitutions, mtime=mtime)
                if cache and not substitutions:
                    cache.put(abspath, tfile)
                
        except (io.FileLoadError, OSError, FilenameAttributeError), xerror:
            failures.append(abspath)
            logger.warn(xerror)
        else:
            yield tfile
        
        if pbar: pbar.update(ifile+1)
    
    if pbar: pbar.finish()
    if failures:
        logger.warn('The following file%s caused problems and will be ignored:\n' % util.plural_s(len(failures)) + '\n'.join(failures))
    
    if cache:
        cache.dump_modified()

class TracesGroup(object):
    
    '''Trace container base class.
    
    Base class for Pile, SubPile, and TracesFile, i.e. anything containing 
    a collection of several traces. A TracesGroup object maintains lookup sets
    of some of the traces meta-information, as well as a combined time-range
    of its contents.
    '''
    
    
    def __init__(self, parent):
        self.parent = parent
        self.empty()
        self.nupdates = 0
    
    def set_parent(self, parent):
        self.parent = parent
    
    def get_parent(self):
        return self.parent
    
    def empty(self):
        self.networks, self.stations, self.locations, self.channels, self.nslc_ids = [ set() for x in range(5) ]
        self.tmin, self.tmax = None, None
        self.have_tuples = False
    
    def update(self, content, empty=True):
        if empty:
            self.empty()
        else:
            if self.have_tuples:
                self._convert_tuples_to_sets()
            
        for c in content:
        
            if isinstance(c, TracesGroup):
                self.networks.update( c.networks )
                self.stations.update( c.stations )
                self.locations.update( c.locations )
                self.channels.update( c.channels )
                self.nslc_ids.update( c.nslc_ids )
                
            elif isinstance(c, trace.Trace):
                self.networks.add(c.network)
                self.stations.add(c.station)
                self.locations.add(c.location)
                self.channels.add(c.channel)
                self.nslc_ids.add(c.nslc_id)
            
            if self.tmin is None:
                self.tmin = c.tmin
            else:
                self.tmin = min(self.tmin, c.tmin)
                
            if self.tmax is None:
                self.tmax = c.tmax
            else:
                self.tmax = max(self.tmax, c.tmax)
        
        if empty:    
            self._convert_small_sets_to_tuples()
        
        self.nupdates += 1
    
    def notify_listeners(self, what):
        pass
    
    def recursive_grow_update(self, content=None):
        
        if content is not None:
            self.update(content, empty=False)
        
        if self.parent is not None:
            self.parent.recursive_grow_update((self,))
            
        self.notify_listeners('update')
    
    def recursive_full_update(self):
        assert False, 'should be implemented in derived class'
        
    def get_update_count(self):
        return self.nupdates
    
    def overlaps(self, tmin,tmax):
        #return not (tmax < self.tmin or self.tmax < tmin)
        return tmax >= self.tmin and self.tmax >= tmin
    
    def is_relevant(self, tmin, tmax, group_selector=None):
        #return  not (tmax <= self.tmin or self.tmax < tmin) and (selector is None or selector(self))
        return  tmax >= self.tmin and self.tmax >= tmin and (group_selector is None or group_selector(self))

    def _convert_tuples_to_sets(self):
        if not isinstance(self.networks, set):
            self.networks = set(self.networks)
        if not isinstance(self.stations, set):
            self.stations = set(self.stations)
        if not isinstance(self.locations, set):
            self.locations = set(self.locations)
        if not isinstance(self.channels, set):
            self.channels = set(self.channels)
        if not isinstance(self.nslc_ids, set):
            self.nslc_ids = set(self.nslc_ids)
        self.have_tuples = False

    def _convert_small_sets_to_tuples(self):
        if len(self.networks) < 32:
            self.networks = reuse(tuple(self.networks))
            self.have_tuples = True
        if len(self.stations) < 32:
            self.stations = reuse(tuple(self.stations))
            self.have_tuples = True
        if len(self.locations) < 32:
            self.locations = reuse(tuple(self.locations))
            self.have_tuples = True
        if len(self.channels) < 32:
            self.channels = reuse(tuple(self.channels))
            self.have_tuples = True
        if len(self.nslc_ids) < 32:
            self.nslc_ids = reuse(tuple(self.nslc_ids))
            self.have_tuples = True
            
class MemTracesFile(TracesGroup):
    
    '''This is needed to make traces without an actual disc file to be inserted
    into a Pile.'''
    
    def __init__(self, parent, traces):
        TracesGroup.__init__(self, parent)
        self.traces = traces
        self.update(self.traces)
        self.mtime = time.time()
        
    def load_headers(self, mtime=None):
        pass
        
    def load_data(self):
        pass
        
    def use_data(self):
        pass
        
    def drop_data(self):
        pass
        
    def reload_if_modified(self):
        pass
        
    def recursive_full_update(self):
        self.update(self.traces)
        
        if self.parent is not None:
            self.parent.recursive_full_update()
        
        self.notify_listeners('fullupdate')
            
    def get_newest_mtime(self, tmin, tmax, trace_selector=None):
        mtime = None
        for tr in self.traces:
            if not trace_selector or trace_selector(tr):
                mtime = max(mtime, self.mtime)
                
        return mtime
        
    def chop(self,tmin,tmax,trace_selector=None, snap=(round,round), load_data=True):
        chopped = []
        used = False
        needed = [ tr for tr in self.traces if not trace_selector or trace_selector(tr) ]
                
        if needed:
            if load_data:
                used = True

            for tr in self.traces:
                if not trace_selector or trace_selector(tr):
                    try:
                        chopped.append(tr.chop(tmin,tmax,inplace=False,snap=snap))
                    except trace.NoData:
                        pass
            
        return chopped, used
        
    def get_deltats(self):
        deltats = set()
        for trace in self.traces:
            deltats.add(trace.deltat)
            
        return deltats
    
    def iter_traces(self):
        for trace in self.traces:
            yield trace
    
    def get_traces(self):
        return self.traces
    
    def gather_keys(self, gather, selector=None):
        keys = set()
        for trace in self.traces:
            if selector is None or selector(trace):
                keys.add(gather(trace))
            
        return keys
    
    def __str__(self):
        def sl(s):
            return sorted(list(s))
        
        s = 'MemTracesFile\n'
        s += 'abspath: %s\n' % self.abspath
        s += 'file mtime: %s\n' % util.gmctime(self.mtime)
        s += 'number of traces: %i\n' % len(self.traces)
        s += 'timerange: %s - %s\n' % (util.gmctime(self.tmin), util.gmctime(self.tmax))
        s += 'networks: %s\n' % ', '.join(sl(self.networks))
        s += 'stations: %s\n' % ', '.join(sl(self.stations))
        s += 'locations: %s\n' % ', '.join(sl(self.locations))
        s += 'channels: %s\n' % ', '.join(sl(self.channels))
        return s

class TracesFile(TracesGroup):
    def __init__(self, parent, abspath, format, substitutions=None, mtime=None):
        TracesGroup.__init__(self, parent)
        self.abspath = abspath
        self.format = format
        self.traces = []
        self.data_loaded = False
        self.data_use_count = 0
        self.substitutions = substitutions
        self.load_headers(mtime=mtime)
        self.update(self.traces)
        self.mtime = mtime
        
    def recursive_full_update(self):
        self.update(self.traces)
        
        if self.parent is not None:
            self.parent.recursive_full_update()
        
        self.notify_listeners('fullupdate')
        
    def load_headers(self, mtime=None):
        logger.debug('loading headers from file: %s' % self.abspath)
        if mtime is None:
            self.mtime = os.stat(self.abspath)[8]
        
        self.traces = []
        for tr in io.load(self.abspath, format=self.format, getdata=False, substitutions=self.substitutions):
            self.traces.append(tr)
            
        self.data_loaded = False
        self.data_use_count = 0
        
    def load_data(self, force=False):
        if not self.data_loaded or force:
            logger.debug('loading data from file: %s' % self.abspath)
            self.traces = []
            for tr in io.load(self.abspath, format=self.format, getdata=True, substitutions=self.substitutions):
                self.traces.append(tr)
                
            self.data_loaded = True
    
    def use_data(self):
        if not self.data_loaded: raise Exception('Data not loaded')
        self.data_use_count += 1
        
    def drop_data(self):
        if self.data_loaded:
            if self.data_use_count == 1:
                logger.debug('forgetting data of file: %s' % self.abspath)
                for tr in self.traces:
                    tr.drop_data()
                    
                self.data_loaded = False
                    
            self.data_use_count -= 1    
        else:
            self.data_use_count = 0
            
    def reload_if_modified(self):
        mtime = os.stat(self.abspath)[8]
        if mtime != self.mtime:
            logger.debug('mtime=%i, reloading file: %s' % (mtime, self.abspath))
            self.mtime = mtime
            if self.data_loaded:
                self.load_data(force=True)
            else:
                self.load_headers()
            
            self.update(self.traces)
            
            return True
            
        return False
       
    def get_newest_mtime(self, tmin, tmax, trace_selector=None):
        mtime = None
        for tr in self.traces:
            if not trace_selector or trace_selector(tr):
                mtime = max(mtime, self.mtime)
                
        return mtime

    def chop(self,tmin,tmax,trace_selector=None, snap=(round,round), load_data=True):
        chopped = []
        used = False
        needed = [ tr for tr in self.traces if not trace_selector or trace_selector(tr) ]
                
        if needed:
            if load_data:
                self.load_data()
                used = True

            for tr in self.traces:
                if not trace_selector or trace_selector(tr):
                    try:
                        chopped.append(tr.chop(tmin,tmax,inplace=False,snap=snap))
                    except trace.NoData:
                        pass
            
        return chopped, used
        
    def get_deltats(self):
        deltats = set()
        for trace in self.traces:
            deltats.add(trace.deltat)
            
        return deltats
    
    def iter_traces(self):
        for trace in self.traces:
            yield trace
    
    def gather_keys(self, gather, selector=None):
        keys = set()
        for trace in self.traces:
            if selector is None or selector(trace):
                keys.add(gather(trace))
            
        return keys
    
    def __str__(self):
        
        def sl(s):
            return sorted(list(s))
        
        s = 'TracesFile\n'
        s += 'abspath: %s\n' % self.abspath
        s += 'file mtime: %s\n' % util.gmctime(self.mtime)
        s += 'number of traces: %i\n' % len(self.traces)
        s += 'timerange: %s - %s\n' % (util.gmctime(self.tmin), util.gmctime(self.tmax))
        s += 'networks: %s\n' % ', '.join(sl(self.networks))
        s += 'stations: %s\n' % ', '.join(sl(self.stations))
        s += 'locations: %s\n' % ', '.join(sl(self.locations))
        s += 'channels: %s\n' % ', '.join(sl(self.channels))
        return s


    
class FilenameAttributeError(Exception):
    pass

class SubPile(TracesGroup):
    def __init__(self, parent):
        TracesGroup.__init__(self, parent)
        self.files = []
        self.empty()
        
    def recursive_full_update(self):
        self.update(self.files)
        
        if self.parent is not None:
            self.parent.recursive_full_update()
        
        self.notify_listeners('fullupdate')
    
    def add_file(self, file):
        self.files.append(file)
        file.set_parent(self)
        self.update((file,), empty=False)
        
    def remove_file(self, file):
        self.files.remove(file)
        file.set_parent(None)
        self.update(self.files)
    
    def remove_files(self, files):
        for file in files:
            self.files.remove(file)
            file.set_parent(None)
        self.update(self.files)
    
    def get_newest_mtime(self, tmin, tmax, group_selector=None, trace_selector=None):
        mtime = None
        for file in self.files:
            if file.is_relevant(tmin, tmax, group_selector):
                mtime = max(mtime, file.get_newest_mtime(tmin, tmax, trace_selector))
                
        return mtime
    
    def chop(self, tmin, tmax, group_selector=None, trace_selector=None, snap=(round,round), load_data=True):
        used_files = set()
        chopped = []
        for file in self.files:
            if file.is_relevant(tmin, tmax, group_selector):
                chopped_, used = file.chop(tmin, tmax, trace_selector, snap, load_data)
                chopped.extend( chopped_ )
                if used:
                    used_files.add(file)
                
        return chopped, used_files
        
    def gather_keys(self, gather, selector=None):
        keys = set()
        for file in self.files:
            keys |= file.gather_keys(gather, selector)
            
        return keys

    def get_deltats(self):
        deltats = set()
        for file in self.files:
            deltats.update(file.get_deltats())
            
        return deltats

    def iter_traces(self, load_data=False, return_abspath=False, group_selector=None, trace_selector=None):
        for file in self.files:
            
            if group_selector and not group_selector(file):
                continue
            
            must_drop = False
            if load_data:
                file.load_data()
                file.use_data()
                must_drop = True
            
            for trace in file.iter_traces():
                if trace_selector and not trace_selector(trace):
                    continue
                
                if return_abspath:
                    yield file.abspath, trace
                else:
                    yield trace
            
            if must_drop:
                file.drop_data()

    def iter_files(self):
        for file in self.files:
            yield file
            
    def reload_modified(self):
        modified = False
        for file in self.files:
            modified |= file.reload_if_modified()
        
        if modified:
            self.update(self.files)
            
        return modified
        
    def __str__(self):
    
        def sl(s):
            return sorted([ x for x in s ])

        s = 'SubPile\n'
        s += 'number of files: %i\n' % len(self.files)
        s += 'timerange: %s - %s\n' % (util.gmctime(self.tmin), util.gmctime(self.tmax))
        s += 'networks: %s\n' % ', '.join(sl(self.networks))
        s += 'stations: %s\n' % ', '.join(sl(self.stations))
        s += 'locations: %s\n' % ', '.join(sl(self.locations))
        s += 'channels: %s\n' % ', '.join(sl(self.channels))
        return s

             
class Pile(TracesGroup):
    def __init__(self):
        TracesGroup.__init__(self, None)
        self.subpiles = {}
        self.update(self.subpiles.values())
        self.open_files = {}
        self.listeners = []
        
    def recursive_full_update(self):
        self.update(self.subpiles.values())
        self.notify_listeners('fullupdate')
    
    def add_listener(self, obj):
        self.listeners.append(weakref.ref(obj))
    
    def notify_listeners(self, what):
        for ref in self.listeners:
            obj = ref()
            if obj:
                obj.pile_changed(what)
    
    def load_files(self, filenames, filename_attributes=None, fileformat='mseed', cache=None, show_progress=True):
        l = loader(filenames, fileformat, cache, filename_attributes, show_progress=show_progress)
        self.add_files(l)
        
    def add_files(self, files):
        modified_subpiles = set()
        for file in files:
            subpile = self.dispatch(file)
            subpile.add_file(file)
            modified_subpiles.add(subpile)
        
        self.update(modified_subpiles, empty=False)
        self.notify_listeners('add')
        
    def add_file(self, file):
        subpile = self.dispatch(file)
        subpile.add_file(file)
        self.update((file,), empty=False)
        self.notify_listeners('add')
    
    def remove_file(self, file):
        subpile = file.get_parent()
        subpile.remove_file(file)
        self.update(self.subpiles.values())
        self.notify_listeners('remove')
        
    def remove_files(self, files):
        subpile_files = {}
        for file in files:
            subpile = file.get_parent()
            if subpile not in subpile_files:
                subpile_files[subpile] = []
            
            subpile_files[subpile].append(file)
       
        for subpile, files in subpile_files.iteritems():
            subpile.remove_files(files)
            
        self.update(self.subpiles.values()) 
        self.notify_listeners('remove')

        
    def dispatch_key(self, file):
       
        
        tt = time.gmtime(int(file.tmin))
        return (tt[0],tt[1])
    
    def dispatch(self, file):
        k = self.dispatch_key(file)
        if k not in self.subpiles:
            self.subpiles[k] = SubPile(self)
            
        return self.subpiles[k]
        
    def get_newest_mtime(self, tmin, tmax, group_selector=None, trace_selector=None):
        mtime = None
        for subpile in self.subpiles.values():
            if subpile.is_relevant(tmin,tmax, group_selector):
                mtime = max(mtime, subpile.get_newest_mtime(tmin, tmax, group_selector, trace_selector))
                
        return mtime
        
    def chop(self, tmin, tmax, group_selector=None, trace_selector=None, snap=(round,round), load_data=True):
        chopped = []
        used_files = set()
        for subpile in self.subpiles.values():
            if subpile.is_relevant(tmin,tmax, group_selector):
                _chopped, _used_files =  subpile.chop(tmin, tmax, group_selector, trace_selector, snap, load_data)
                chopped.extend(_chopped)
                used_files.update(_used_files)
                
        return chopped, used_files

    def _process_chopped(self, chopped, degap, want_incomplete, wmax, wmin, tpad):
        chopped.sort(lambda a,b: cmp(a.full_id, b.full_id))
        if degap:
            chopped = degapper(chopped)
            
        if not want_incomplete:
            wlen = (wmax+tpad)-(wmin-tpad)
            chopped_weeded = []
            for tr in chopped:
                emin = tr.tmin - (wmin-tpad)
                emax = tr.tmax + tr.deltat - (wmax+tpad)
                if (abs(emin) <= 0.5*tr.deltat and 
                    abs(emax) <= 0.5*tr.deltat):
                    chopped_weeded.append(tr)
                elif degap:
                    if (0. < emin <= 5. * tr.deltat and 
                            -5. * tr.deltat <= emax < 0.):
                        tr.extend(wmin-tpad, wmax+tpad-tr.deltat, fillmethod='repeat')
                        chopped_weeded.append(tr)

            chopped = chopped_weeded
        
        for tr in chopped:
            tr.wmin = wmin
            tr.wmax = wmax
        
        return chopped
            
    def chopper(self, tmin=None, tmax=None, tinc=None, tpad=0., group_selector=None, trace_selector=None,
                      want_incomplete=True, degap=True, keep_current_files_open=False, accessor_id=None, snap=(round,round), load_data=True):
        
        if tmin is None:
            tmin = self.tmin+tpad
                
        if tmax is None:
            tmax = self.tmax-tpad
            
        if tinc is None:
            tinc = tmax-tmin
        
        if not self.is_relevant(tmin-tpad,tmax+tpad,group_selector): return
                
        if accessor_id not in self.open_files:
            self.open_files[accessor_id] = set()
                
        open_files = self.open_files[accessor_id]
        
        iwin = 0
        while True:
            chopped = []
            wmin, wmax = tmin+iwin*tinc, min(tmin+(iwin+1)*tinc, tmax)
            eps = tinc*1e-6
            if wmin >= tmax-eps: break
            chopped, used_files = self.chop(wmin-tpad, wmax+tpad, group_selector, trace_selector, snap, load_data) 
            for file in used_files - open_files:
                # increment datause counter on newly opened files
                file.use_data()
                
            open_files.update(used_files)
            
            processed = self._process_chopped(chopped, degap, want_incomplete, wmax, wmin, tpad)
            yield processed
                        
            unused_files = open_files - used_files
            
            while unused_files:
                file = unused_files.pop()
                file.drop_data()
                open_files.remove(file)
                
            iwin += 1
        
        if not keep_current_files_open:
            while open_files:
                file = open_files.pop()
                file.drop_data()
        
        
    def all(self, *args, **kwargs):
        alltraces = []
        for traces in self.chopper( *args, **kwargs ):
            alltraces.extend( traces )
            
        return alltraces
        
    def iter_all(self, *args, **kwargs):
        for traces in self.chopper( *args, **kwargs):
            for trace in traces:
                yield trace
    
    def chopper_grouped(self, gather, progress=None, *args, **kwargs):
        keys = self.gather_keys(gather)
        if len(keys) == 0: return
        outer_group_selector = None
        if 'group_selector' in kwargs:
            outer_group_selector = kwargs['group_selector']
            
        outer_trace_selector = None
        if 'trace_selector' in kwargs:
            outer_trace_selector = kwargs['trace_selector']
        
        # the use of this gather-cache makes it impossible to modify the pile
        # during chopping
        gather_cache = {}
        pbar = None
        progressbar = util.progressbar_module()
        if progress and progressbar and config.show_progress:
            widgets = [progress, ' ',
                        progressbar.Bar(marker='-',left='[',right=']'), ' ',
                        progressbar.Percentage(), ' ',]
                
            pbar = progressbar.ProgressBar(widgets=widgets, maxval=len(keys)).start()
        
        for ikey, key in enumerate(keys):
            def tsel(tr):
                return gather(tr) == key and (outer_trace_selector is None or 
                                              outer_trace_selector(tr))
                    
            def gsel(gr):
                if gr not in gather_cache:
                    gather_cache[gr] = gr.gather_keys(gather)
                        
                return key in gather_cache[gr] and (outer_group_selector is None or
                                                    outer_group_selector(gr))
            
            kwargs['trace_selector'] = tsel
            kwargs['group_selector'] = gsel
            
            for traces in self.chopper(*args, **kwargs):
                yield traces
                
            if pbar: pbar.update(ikey+1)
        
        if pbar: pbar.finish()
        
    def gather_keys(self, gather, selector=None):
        keys = set()
        for subpile in self.subpiles.values():
            keys |= subpile.gather_keys(gather, selector)
            
        return sorted(keys)
    
    def get_deltats(self):
        deltats = set()
        for subpile in self.subpiles.values():
            deltats.update(subpile.get_deltats())
            
        return sorted(list(deltats))
    
    def iter_traces(self, load_data=False, return_abspath=False, group_selector=None, trace_selector=None):
        for subpile in self.subpiles.values():
            if not group_selector or group_selector(subpile):
                for tr in subpile.iter_traces(load_data, return_abspath, group_selector, trace_selector):
                    yield tr
    
    def iter_files(self):
        for subpile in self.subpiles.values():
            for file in subpile.iter_files():
                yield file
   
    def reload_modified(self):
        modified = False
        for subpile in self.subpiles.values():
            modified |= subpile.reload_modified()
        
        if modified:
            self.update(self.subpiles.values())
            self.notify_listeners('modified')
            
        return modified
    
    def get_tmin(self):
        return self.tmin
        
    def get_tmax(self):
        return self.tmax
    
    def __str__(self):
        
        def sl(s):
            return sorted([ x for x in s ])
        
        s = 'Pile\n'
        s += 'number of subpiles: %i\n' % len(self.subpiles)
        s += 'timerange: %s - %s\n' % (util.gmctime(self.tmin), util.gmctime(self.tmax))
        s += 'networks: %s\n' % ', '.join(sl(self.networks))
        s += 'stations: %s\n' % ', '.join(sl(self.stations))
        s += 'locations: %s\n' % ', '.join(sl(self.locations))
        s += 'channels: %s\n' % ', '.join(sl(self.channels))
        return s
    
    def snuffle(self, **kwargs):
        '''Visualize it.

        :param stations: list of `pyrocko.model.Station` objects or ``None``
        :param events: list of `pyrocko.model.Event` objects or ``None``
        :param markers: list of `pyrocko.gui_util.Marker` objects or ``None``
        :param ntracks: float, number of tracks to be shown initially (default: 12)
        :param follow: time interval (in seconds) for real time follow mode or ``None``
        :param controls: bool, whether to show the main controls (default: ``True``)
        :param opengl: bool, whether to use opengl (default: ``False``)
        '''

        from pyrocko.snuffler import snuffle
        snuffle(self, **kwargs)

def make_pile( paths=None, selector=None, regex=None,
        fileformat = 'mseed',
        cachedirname='/tmp/pyrocko_cache_%s' % os.environ['USER'], show_progress=True ):
    
    '''Create pile from given file and directory names.
    
    :param paths: filenames and/or directories to look for traces. If paths is 
        ``None`` ``sys.argv[1:]`` is used.
    :param selector: lambda expression taking group dict of regex match object as
        a single argument and which returns true or false to keep or reject
        a file
    :param regex: regular expression which filenames have to match
    :param fileformat: format of the files ('mseed', 'sac', 'kan', 
        'from_extension', 'try')
    :param cachedirname: loader cache is stored under this directory. It is
        created as neccessary.
    :param show_progress: show progress bar and other progress information
    '''
    if isinstance(paths, str):
        paths = [ paths ]
        
    if paths is None:
        paths = sys.argv[1:]
    
    fns = util.select_files(paths, selector, regex, show_progress=show_progress)

    cache = get_cache(cachedirname)
    p = Pile()
    p.load_files( sorted(fns), cache=cache, fileformat=fileformat, show_progress=show_progress)
    return p



