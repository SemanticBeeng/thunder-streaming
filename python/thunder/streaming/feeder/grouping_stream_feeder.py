#!/usr/bin/env python
"""An elaboration on stream_feeder that watches for pairs of files with matching suffixes. Only when a matching
pair is found will both files be moved into the output directory.

When run as a script, this file expects to find matching files in two separate directory trees. These files are
assumed to represent imaging and behavioral data from the same point in time.

Files are matched based on having identical suffixes after the first appearance of a delimiter character '_', excluding
filename extensions. So 'foo_abc.txt' and 'bar_abc' match, but 'foo_123' and 'bar_124' do not.

Note that this script will block forever waiting for a match. So for instance given files a_01, a_02, a_03, b_01, and
b_03, after moving the a_01 b_01 pair it will block waiting for a b_02 to show up.

"""

import logging
import os
import sys
import time
from collections import deque
from itertools import imap, izip, groupby, tee
from itertools import product as iproduct
from operator import itemgetter

from thunder.streaming.feeder.utils.feeder_logger import _logger
from thunder.streaming.feeder.utils.feeder_regex import RegexMatchToQueueName, RegexMatchToTimepointString
from stream_feeder import build_filecheck_generators, runloop, CopyAndMoveFeeder


def unique_justseen(iterable, key=None):
    """List unique elements, preserving order. Remember only the element just seen.
    Taken from python itertools recipes.
    """
    # unique_justseen('AAAABBBCCDAABBB') --> A B C D A B
    # unique_justseen('ABBCcAD', str.lower) --> A B C A D
    return imap(next, imap(itemgetter(1), groupby(iterable, key)))


# next two functions from stackoverflow user hughdbrown:
# http://stackoverflow.com/questions/3755136/pythonic-way-to-check-if-a-list-is-sorted-or-not/4404056#4404056
def pairwise(iterable):
    a, b = tee(iterable)
    next(b, None)
    return izip(a, b)


# tests for strict ordering, will be false for dups
def is_sorted(iterable, key=lambda a, b: a < b):
    return all(key(a, b) for a, b in pairwise(iterable))


def getFilenamePrefix(filename, delim='_'):
    return getFilenamePrefixAndPostfix(filename, delim)[0]


def getFilenamePostfix(filename, delim='_'):
    return getFilenamePrefixAndPostfix(filename, delim)[1]


def getFilenamePrefixAndPostfix(filename, delim='_'):
    bname = os.path.splitext(os.path.basename(filename))[0]
    splits = bname.split(delim, 1)
    prefix = splits[0]
    postfix = splits[1] if len(splits) > 1 else ''
    return prefix, postfix


class SyncCopyAndMoveFeeder(CopyAndMoveFeeder):
    """This feeder will wait for matching pairs of files, as described in the module docstring,
    before copying the pair into the passed output directory. Its behavior is otherwise the
    same as CopyAndMoveFeeder.

    Filenames that are not immediately matched on a first call to feed() are stored in internal queues,
    to be checked on the next feed() call. The internal queues are sorted alphabetically by file name, and
    at each feed() call only the head of the queue is checked for a possible match. This can lead to
    waiting forever for a match for one particular file, as described in the module docstring.
    """
    def __init__(self, feeder_dir, linger_time, qnames,
                 fname_to_qname_fcn=getFilenamePrefix,
                 fname_to_timepoint_fcn=getFilenamePostfix,
                 check_file_size_mismatch=False,
                 check_skip_in_sequence=True):
        super(SyncCopyAndMoveFeeder, self).__init__(feeder_dir=feeder_dir, linger_time=linger_time)
        self.qname_to_queue = {}
        for qname in qnames:
            self.qname_to_queue[qname] = deque()
        self.keys_to_fullnames = {}
        self.fname_to_qname_fcn = fname_to_qname_fcn
        self.fname_to_timepoint_fcn = fname_to_timepoint_fcn
        self.qname_to_expected_size = {} if check_file_size_mismatch else None
        self.do_check_sequence = check_skip_in_sequence
        self.last_timepoint = None
        self.last_mismatch = None
        self.last_mismatch_time = None
        # time in s to wait after detecting a mismatch before popping mismatching elements:
        self.mismatch_wait_time = 5.0

    def check_and_pop_mismatches(self, first_elts):
        """Checks for a mismatched first elements across queues.

        If the first mismatched element has remained the same for longer than
        self.mismatch_wait_time, then start popping out mismatching elements.

        Updates self.last_mismatch and self.last_mismatch_time
        """
        comp_elt = first_elts[0]
        # the below list comprehension Does The Right Thing for first_elts of length 1
        # and returns [], and all([]) is True.
        if comp_elt is not None and all([elt == comp_elt for elt in first_elts[1:]]):
            matched = comp_elt
            self.last_mismatch = None
            self.last_mismatch_time = None
        else:
            matched = None
            # this returns None if there are any Nones in the list:
            cur_mismatch = reduce(min, first_elts)
            if cur_mismatch is None:
                # if there is at least one None, then there is some empty queue
                # we don't consider it a mismatch unless there are first elements in each queue, and
                # they don't match - so if a queue is empty, we don't have a mismatch
                self.last_mismatch = None
                self.last_mismatch_time = None
            else:
                now = time.time()
                if self.last_mismatch:  # we already had a mismatch last time through, presumably on the same elt
                    if self.last_mismatch != cur_mismatch:
                        # blow up
                        raise Exception("Current mismatch '%s' doesn't match last mismatch '%s' " %
                                        (cur_mismatch, self.last_mismatch) + "- this shouldn't happen")
                    if now - self.last_mismatch_time > self.mismatch_wait_time:
                        # we have been stuck on this element for longer than mismatch_wait_time
                        # find the next-lowest element - this is not None, since the other queues are not empty
                        next_elts = first_elts[:]  # copy
                        next_elts.remove(cur_mismatch)
                        next_elt = reduce(min, next_elts)
                        # cycle through *all* queues, removing any elts less than next_elt
                        popping = True
                        while popping:
                            popping = False
                            for qname, q in self.qname_to_queue.iteritems():
                                if q and q[0] < next_elt:
                                    discard = q.popleft()
                                    popping = True
                                    _logger.get().warn("Discarding item '%s' from queue '%s'; " % (discard, qname) +
                                                       "waited for match for more than %g s" % self.mismatch_wait_time)
                        # finished popping all mismatching elements less than next_elt
                        # we might have a match at this point, but wait for next iteration to pick up
                        self.last_mismatch = None
                        self.last_mismatch_time = None
                else:
                    self.last_mismatch = cur_mismatch
                    self.last_mismatch_time = now
        return matched

    def get_matching_first_entry(self):
        """Pops and returns the first entry across all queues if the first entry
        is the same on all queues, otherwise return None and leave queues unchanged
        """
        first_elts = []
        for q in self.qname_to_queue.itervalues():
            first_elts.append(q[0] if q else None)

        matched = self.check_and_pop_mismatches(first_elts)

        if matched is not None:
            for queue in self.qname_to_queue.itervalues():
                queue.popleft()
        return matched

    def filter_size_mismatch_files(self, filenames):
        filtered_timepoints = []
        for filename in filenames:
            size = os.path.getsize(filename)
            bname = os.path.basename(filename)
            queuename = self.fname_to_qname_fcn(bname)
            timepoint = self.fname_to_timepoint_fcn(bname)
            expected_size = self.qname_to_expected_size.setdefault(queuename, size)
            if size != expected_size:
                filtered_timepoints.append(timepoint)
                _logger.get().warn(
                    "Size mismatch on '%s', discarding timepoint '%s'. (Expected %d bytes, got %d bytes.)",
                    filename, timepoint, expected_size, size)
        if filtered_timepoints:
            return [filename for filename in filenames if
                    self.fname_to_timepoint_fcn(os.path.basename(filename)) not in filtered_timepoints]
        else:
            return filenames

    def check_sequence(self, timepoint_string):
        if self.last_timepoint is None:
            self.last_timepoint = int(timepoint_string)
            return
        cur_timepoint = int(timepoint_string)
        if cur_timepoint != self.last_timepoint + 1:
            _logger.get().warn("Missing timepoints detected, went from '%d' to '%d'",
                               self.last_timepoint, cur_timepoint)
        self.last_timepoint = cur_timepoint

    def match_filenames(self, filenames):
        """Update internal queues with passed filenames. Returns names that match across the head of all queues if
        any are found, or an empty list otherwise.
        """
        # insert
        # we assume that usually we'll just be appending to the end - other options
        # include heapq and bisect, but it probably doesn't really matter
        for filename in filenames:
            qname = self.fname_to_qname_fcn(filename)
            if qname is None:
                _logger.get().warn("Could not get queue name for file '%s', skipping" % filename)
                continue
            tpname = self.fname_to_timepoint_fcn(filename)
            if tpname is None:
                _logger.get().warn("Could not get timepoint for file '%s', skipping" % filename)
                continue
            self.qname_to_queue[qname].append(tpname)
            self.keys_to_fullnames[(qname, tpname)] = filename

        # maintain sorting and dedup:
        for qname, queue in self.qname_to_queue.iteritems():
            if not is_sorted(queue):
                self.qname_to_queue[qname] = deque(unique_justseen(sorted(list(queue))))

        # all queues are now sorted and unique-ified

        # check for matching first entries across queues
        matching = self.get_matching_first_entry()
        matches = []
        dcs = self.do_check_sequence
        while matching:
            if dcs:
                self.check_sequence(matching)
            matches.append(matching)
            matching = self.get_matching_first_entry()

        # convert matches back to full filenames
        fullnamekeys = list(iproduct(self.qname_to_queue.iterkeys(), matches))
        fullnames = [self.keys_to_fullnames.pop(key) for key in fullnamekeys]
        fullnames.sort()

        # filter out files that are smaller than the first file to be added to the queue, if requested
        # this attempts to check for and work around an error state where some files are incompletely
        # transferred
        if self.qname_to_expected_size is not None:
            fullnames = self.filter_size_mismatch_files(fullnames)

        return fullnames

    def feed(self, filenames):
        fullnames = self.match_filenames(filenames)
        return super(SyncCopyAndMoveFeeder, self).feed(fullnames)


def parse_options():
    import optparse
    parser = optparse.OptionParser(usage="%prog imgdatadir behavdatadir outdir [options]")
    parser.add_option("-p", "--poll-time", type="float", default=1.0,
                      help="Time between checks of datadir in s, default %default")
    parser.add_option("-m", "--mod-buffer-time", type="float", default=1.0,
                      help="Time to wait after last file modification time before feeding file into stream, "
                           "default %default")
    parser.add_option("-l", "--linger-time", type="float", default=5.0,
                      help="Time to wait after feeding into stream before deleting intermediate file "
                           "(negative time disables), default %default")
    parser.add_option("--max-files", type="int", default=-1,
                      help="Max files to copy in one iteration "
                           "(negative disables), default %default")
    parser.add_option("--imgprefix", default="img")
    parser.add_option("--behavprefix", default="behav")
    parser.add_option("--prefix-regex-file", default=None)
    parser.add_option("--timepoint-regex-file", default=None)
    opts, args = parser.parse_args()

    if len(args) != 3:
        print >> sys.stderr, parser.get_usage()
        sys.exit(1)

    setattr(opts, "imgdatadir", args[0])
    setattr(opts, "behavdatadir", args[1])
    setattr(opts, "outdir", args[2])

    return opts


def get_parsing_functions(opts):
    if opts.prefix_regex_file:
        fname_to_qname_fcn = RegexMatchToQueueName.fromFile(opts.prefix_regex_file).queueName
    else:
        fname_to_qname_fcn = getFilenamePrefix
    if opts.timepoint_regex_file:
        fname_to_timepoint_fcn = RegexMatchToTimepointString.fromFile(opts.timepoint_regex_file).timepoint
    else:
        fname_to_timepoint_fcn = getFilenamePostfix
    return fname_to_qname_fcn, fname_to_timepoint_fcn


def main():
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(asctime)s:%(message)s'))
    _logger.get().addHandler(_handler)
    _logger.get().setLevel(logging.INFO)

    opts = parse_options()

    fname_to_qname_fcn, fname_to_timepoint_fcn = get_parsing_functions(opts)
    feeder = SyncCopyAndMoveFeeder(opts.outdir, opts.linger_time, (opts.imgprefix, opts.behavprefix),
                                   fname_to_qname_fcn=fname_to_qname_fcn,
                                   fname_to_timepoint_fcn=fname_to_timepoint_fcn)

    file_checkers = build_filecheck_generators((opts.imgdatadir, opts.behavdatadir), opts.mod_buffer_time,
                                               max_files=opts.max_files,
                                               filename_predicate=fname_to_qname_fcn)
    runloop(file_checkers, feeder, opts.poll_time)

if __name__ == "__main__":
    main()