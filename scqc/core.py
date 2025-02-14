#!/usr/bin/env python
#
#

import argparse
import fcntl
import io
import logging
import os
import tempfile
import time
import traceback

from configparser import ConfigParser
from queue import Queue

from scqc import sra, star
from scqc.utils import *


def get_default_config():
    cp = ConfigParser()
    cp.read(os.path.expanduser("~/git/scqc/etc/scqc.conf"))
    return cp


class Stage(object):
    '''
    Handles stage in pipeline. 
    Reads donelist. Reads todolist. Calculates diff. 
    Executes all for difflist. 
    Writes updated donelist. 

    '''

    def __init__(self, config, name):
        self.name = name
        self.log = logging.getLogger(self.name)
        self.log.info(f'{self.name} init...')
        self.config = config
        self.todofile = self.config.get(f'{self.name}', 'todofile')
        if self.todofile.lower().strip() == "none":
            self.todofile = None
        else:
            self.todofile = os.path.expanduser(self.todofile)

        self.donefile = self.config.get(f'{self.name}', 'donefile')
        if self.donefile.lower().strip() == "none":
            self.donefile = None
        else:
            self.donefile = os.path.expanduser(self.donefile)
        self.shutdown = False
        self.sleep = int(self.config.get(f'{self.name}', 'sleep'))
        self.batchsize = int(self.config.get(f'{self.name}', 'batchsize'))
        self.batchsleep = float(self.config.get(f'{self.name}', 'batchsleep'))
        self.ncycles = int(self.config.get(f'{self.name}', 'ncycles'))
        self.outlist = []

    def run(self):
        self.log.info(f'{self.name} run...')
        cycles = 0
        try:
            while not self.shutdown:
                self.log.debug(
                    f'{self.name} cycle will be {self.sleep} seconds...')
                self.todolist = readlist(self.todofile)
                self.donelist = readlist(self.donefile)
                if self.todolist is not None:
                    self.dolist = listdiff(self.todolist, self.donelist)
                else:
                    self.dolist = []
                # cut into batches and do each separately, updating donelist. 
                logging.debug(f'dolist len={len(self.dolist)}')
                curid = 0
                while curid < len(self.dolist):
                    dobatch = self.dolist[curid:curid + self.batchsize]
                    logging.debug(f'made dobatch length={len(dobatch)}')
                    logging.debug(f'made dobatch: {dobatch}')
                    finished = self.execute(dobatch)
                    try:
                        finished.remove(None)
                    except:
                        self.log.warn('Got None in finished list from an execute. Removed.')
                    self.log.debug(f"got finished list len={len(finished)}. writing...")
                    
                    if self.donefile is not None and len(finished) > 0:
                        logging.info('reading current done.')
                        donelist = readlist(self.donefile)
                        logging.info('adding just finished.')
                        alldone = listmerge(finished, donelist)
                        writelist(self.donefile, alldone)
                        self.log.debug(
                            f"done writing donelist: {self.donefile}. sleeping {self.batchsleep} ...")
                    else:
                        logging.info(
                            'donefile is None or no new processing. No output.')
                        
                    curid += self.batchsize
                    time.sleep(self.batchsleep)
                cycles += 1
                if cycles >= self.ncycles:
                    self.shutdown = True

                # overall stage sleep
                if not self.shutdown:
                    logging.info(f'done with all batches. Sleeping for stage. {self.sleep} sec...')
                    time.sleep(self.sleep)

        except KeyboardInterrupt:
            print('\nCtrl-C. stopping.')

        except Exception as ex:
            self.log.warning("exception raised during main loop.")
            self.log.error(traceback.format_exc(None))
            raise ex
        logging.info(f'Shutdown set. Exitting {self.name}')


    def stop(self):
        self.log.info('stopping...')


class Query(Stage):
    """
    Stage takes in list of NCBI project ids. 
    Collects metadata on projects, samples, experiments, and runs. Stores in DFs. 
    Outputs complete project ids. 
    """

    def __init__(self, config):
        super(Query, self).__init__(config, 'query')
        self.log.debug('super() ran. object initialized.')

    def execute(self, dolist):
        '''
        Perform one run for stage.  
        '''
        self.log.debug(f'got dolist len={len(dolist)}. executing...')
        outlist = []
        for projectid in dolist:
            self.log.debug(f'handling id {projectid}...')
            try:
                sq = sra.Query(self.config)
                out = sq.execute(projectid)
                self.log.debug(f'done with {projectid}')
                outlist.append(out)
            except Exception as ex:
                self.log.warning(f"exception raised during project query: {projectid}")
                self.log.error(traceback.format_exc(None))
        self.log.debug(f"returning outlist len={len(outlist)}")
        return outlist

    def setup(self):
        sra.setup(self.config)

class Impute(Stage):
    """
    Stage takes in list of NCBI project ids. 
    Examines Library Construction Protocol, and where needed downloads first X kilobytes of run files to guess
    library technology. 
    
    Outputs complete project ids. 
    
    """
    def __init__(self, config):
        super(Impute, self).__init__(config, 'impute')
        self.log.debug('super() ran. object initialized.')

    def execute(self, dolist):
        '''
        Perform one run for stage.  
        '''
        self.log.debug(f'got dolist len={len(dolist)}. executing...')
        outlist = []
        for projectid in dolist:
            self.log.debug(f'handling id {projectid}...')
            try:
                si = sra.Impute(self.config)
                out = si.execute(projectid)
                self.log.debug(f'done with {projectid}')
                if out is not None:
                    outlist.append(out)
            
            except Exception as ex:
                self.log.warning(f"exception raised during project query: {projectid}")
                self.log.error(traceback.format_exc(None))
        self.log.debug(f"returning outlist len={len(outlist)}")
        return outlist

    def setup(self):
        sra.setup(self.config)


class Download(Stage):

    def __init__(self, config):
        super(Download, self).__init__(config, 'download')
        self.log.debug('super() ran. object initialized.')
        self.max_downloads = int(self.config.get('download', 'max_downloads'))
        self.num_streams = int(self.config.get('download', 'num_streams'))

    def execute(self, dolist):
        '''
        Perform one run for stage.  
        '''
        self.log.debug(f'executing {self.name}')
        outlist = []
        runlist = []
        dq = Queue()
        for projectid in dolist:
            runids = sra.get_runs_for_project(self.config, projectid)
            self.log.debug(f'got runids to prefetch: {runids}')
            for runid in runids:
                projfetch = sra.ProjectPrefetch(self.config, projid, outlist)
                #pf = sra.Prefetch(self.config, runid, outlist)
                dq.put(pf)
            outlist.append(projectid)
        logging.debug(f'created queue of {dq.qsize()} items')
        md = int(self.config.get('sra', 'max_downloads'))
        for n in range(md):
            sra.Worker(dq).start()
        logging.debug('waiting to join threads...')
        dq.join()
        logging.debug('all workers done...')
        logging.info(f'prefetched runs: {runlist}')
        return outlist


    def setup(self):
        sra.setup(self.config)


class Analysis(Stage):

    def __init__(self, config):
        super(Analysis, self).__init__(config, 'analysis')
        self.log.debug('super() ran. object initialized.')

    def execute(self):
        pass

    def setup(self):
        star.setup(self.config)


class Statistics(Stage):

    def __init__(self, config):
        super(Statistics, self).__init__(config, 'analysis')
        self.log.debug('super() ran. object initialized.')

    def execute(self):
        pass

    def setup(self):
        pass




class CLI(object):

    def parseopts(self):

        FORMAT = '%(asctime)s (UTC) [ %(levelname)s ] %(filename)s:%(lineno)d %(name)s.%(funcName)s(): %(message)s'
        logging.basicConfig(format=FORMAT)

        parser = argparse.ArgumentParser()

        parser.add_argument('-d', '--debug',
                            action="store_true",
                            dest='debug',
                            help='debug logging')

        parser.add_argument('-v', '--verbose',
                            action="store_true",
                            dest='verbose',
                            help='verbose logging')

        parser.add_argument('-c', '--config',
                            action="store",
                            dest='conffile',
                            default='~/git/scqc/etc/scqc.conf',
                            help='Config file path [~/git/scqc/etc/scqc.conf]')

        parser.add_argument('-s', '--setup',
                            action="store_true",
                            dest='setup',
                            help='perform setup for chosen daemon and exit...'
                            )
        parser.add_argument('-n','--ncycles',
                            action='store',
                            dest='ncycles',
                            default=None,
                            help='halt after N cycles'
                            )


        subparsers = parser.add_subparsers(dest='subcommand',
                                           help='sub-command help.')

        parser_analysis = subparsers.add_parser('query',
                                                help='query daemon')

        parser_analysis = subparsers.add_parser('impute',
                                                help='impute daemon')

        parser_download = subparsers.add_parser('download',
                                                help='download daemon')

        parser_analysis = subparsers.add_parser('analysis',
                                                help='analysis daemon')

        parser_analysis = subparsers.add_parser('statistics',
                                                help='statistics daemon')

        args = parser.parse_args()

        # default to INFO
        logging.getLogger().setLevel(logging.INFO)

        if args.debug:
            logging.getLogger().setLevel(logging.DEBUG)
        if args.verbose:
            logging.getLogger().setLevel(logging.INFO)

        cp = ConfigParser()
        cp.read(os.path.expanduser(args.conffile))
        
        if args.ncycles is not None:
            cp.set('DEFAULT','ncycles',str(int(args.ncycles)))
            
        cs = self.get_configstr(cp)
        logging.debug(f"config: \n{cs} ")
        logging.debug(f"args: {args} ")

        if args.subcommand == 'query':
            d = Query(cp)
            if args.setup:
                d.setup()
            else:
                d.run()

        if args.subcommand == 'impute':
            d = Impute(cp)
            if args.setup:
                d.setup()
            else:
                d.run()

        if args.subcommand == 'download':
            d = Download(cp)
            if args.setup:
                d.setup()
            else:
                d.run()

        if args.subcommand == 'analysis':
            d = Analysis(cp)
            if args.setup:
                d.setup()
            else:
                d.run()

        if args.subcommand == 'statistics':
            d = Statistics(cp)
            if args.setup:
                d.setup()
            else:
                d.run()


    def get_configstr(self, cp):
        with io.StringIO() as ss:
            cp.write(ss)
            ss.seek(0)  # rewind
            return ss.read()

    def run(self):
        self.parseopts()
