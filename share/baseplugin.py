""" BasePlugin definitions. """

import logging
import threading
import traceback
import os
import Queue
from importer import Importer, ImporterError
from sjutils import threadpool

class BasePluginError(Exception):
    """ Raised by BasePlugin. """

    def __init__(self, error):
        """ Init method. """
        Exception.__init__(self, error)

class BasePlugin(threading.Thread):
    """ Base class for job implementation in spvd. """


    def __init__(self, options, event, url=None, params=None):
        """ Init method.

        @url: url pass to Importer.

        @params is a dictionary of optional parameters among:
        importer_retry_timeout: interval between successive importer calls if
                                importer failed.
        max_parallel_checks:    maximum number of threads for this plugin.
        max_checks_queue:       maximum number of checks to get from
                                the DB and queue for execution.
        check_poll:             interval between two get_checks call.
        check_timeout:          maximum wait time for get_checks calls.
        debug:                  enable debugging information.
        ssl_cert:               client X.509 public key.
        ssl_key:                client X.509 secret key.
        result_threshold:       number of results waiting for a commit that
                                will trigger a main-loop wake up.
        """

        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.dismiss    = event
        self.resqueue   = {}
        self.checks     = {}
        self.rescommit  = threading.Event()
        self.name       = ''

        self.params     = { 'importer_retry_timeout': 10,
                            'max_parallel_checks': 3,
                            'max_checks_queue': 9,
                            'check_poll': 1,
                            'check_timeout' : None,
                            'debug': False,
                            'ssl_cert': None,
                            'ssl_key': None,
                            'result_threshold': 5,
                        }

        if params:
            self.params.update(params)

        self.importer   = Importer()
        if url:
            self.importer['distant_url'] = url

        if self.params['ssl_cert'] and self.params['ssl_key']:
            self.importer['ssl_cert'] = self.params['ssl_cert']
            self.importer['ssl_key'] = self.params['ssl_key']

        if self.params['check_timeout']:
            self.importer['timeout'] = self.params['check_timeout']

        self.options = options

        self.log = logging.getLogger(self.name)

        if self.options.nodaemon:
            log_handler = logging.FileHandler('/dev/stdout')
        else:
            log_dir = options.logdir + '/' + self.name
            if os.path.exists(log_dir) is False:
                os.mkdir(log_dir)
            log_handler = logging.FileHandler(log_dir + '/' + self.name + '.log')

        self.log_format = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        log_handler.setFormatter(self.log_format)
        self.log.addHandler(log_handler)

        if self.params.has_key('debug') and self.params['debug'] is True:
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.INFO)

        self.log.propagate = 0

        self.job_pool = threadpool.ThreadPool(int(self.params['max_parallel_checks']))

        self.start()
        self.log.info(self)

    def __str__(self):
        return "<BasePlugin name=%s ssl=%s url=%s>" % (self.name, \
            (self.params['ssl_cert'] and self.params['ssl_key']) and \
            "on" or "off", self.importer['distant_url'] or "localhost")

    @staticmethod
    def __prepare_status_update(check):
        """ Prepare a structure for status update. """
        status = {
            'status_id'     : check['status']['status_id'],
            'sequence_id'   : check['status']['seq_id'],
            'status'        : check['status']['check_status'],
            'message'       : check['status']['check_message'],
            'status_infos'  : check['status']['status_infos'],
        }

        if 'status_infos' in check:
            status['status_infos'] = check['status_infos']

        return status

    def job_start(self, check):
        """ Starts a job. """

        job = self.create_new_job(check)
        job.log.info('check started')
        self.checks[check['status']['status_id']] = job
        return job.run()

    def job_stop(self, request, result):
        """ Stops a job. """

        self.checks[request.request_id].log.info('check result is %s : (%s)' % (result['status']['check_status'], result['status']['check_message']))

        update = self.__prepare_status_update(result)
        self.resqueue.update({result['status']['status_id']: update})

        if len(self.resqueue) > self.params['result_threshold']:
            self.rescommit.set()

        del self.checks[request.request_id]

    def handle_exception(self, request, exc_info):
        """ Handle exception in a job. """

        if not isinstance(exc_info, tuple):
            # Something is seriously wrong...
            self.log.critical('*** Worker thread raised an exception ***')
            self.log.critical(request)
            self.log.critical(exc_info)
            raise SystemExit

        self.log.error("Exception occured in request #%s: %s" % (request.request_id, exc_info))
        for line in traceback.format_exception(exc_info[0], exc_info[1], exc_info[2]):
            self.log.error(line)

    def run(self):
        """ Run method. """

        self.log.info("plugin started")

        while not self.dismiss.isSet():

            try:

                self.rescommit.wait(self.params['check_poll'])

                self.log.debug('number of threads alive %d' % threading.activeCount())
                if self.job_pool._requests_queue.qsize() > 0:
                    self.log.info('approximate number of jobs in queue %d' % self.job_pool._requests_queue.qsize())
                else:
                    self.log.debug('approximate number of jobs in queue %d' % self.job_pool._requests_queue.qsize())

                try:
                    self.job_pool.poll()
                except threadpool.NoResultsPending:
                    self.log.debug('there was no result to poll')

                # Queue.qsize is unreliable, try to mitigate its weirdness
                limit_fetch = self.params['max_checks_queue'] - self.job_pool._requests_queue.qsize()
                limit_fetch = min(abs(limit_fetch), self.params['max_checks_queue'])

                if self.job_pool._requests_queue.full() \
                    or limit_fetch > self.params['max_checks_queue'] \
                    or limit_fetch == 0:
                    # Non sensical value or no check to fetch
                    self.log.info('queue estimated full')
                    continue

                if self.resqueue:
                    self.log.info('%d results to commit' % len(self.resqueue))
                    # Try to commit results in queue
                    try:
                        self.importer.call('spv', 'set_checks_status', self.resqueue.values())
                        self.resqueue = {}
                        self.rescommit.clear()
                    except ImporterError, error:
                        self.log.error('remote module error while commiting updates <' + str(error) + '>')

                # Get checks for the current plugin
                checks = {}
                self.log.debug('*** fetching %s checks' % limit_fetch)

                try:
                    checks = self.importer.call('spv.services', 'get_checks',
                        {'limit' : limit_fetch,
                        'plugin_name' : self.name,
                        'get_check_infos' : True,
                        'get_object_infos' : True,
                        'update_next_check' : True,
                        'next_check_expired' : True})
                except ImporterError, error:
                    self.log.error('remote module error while retrieving checks <' + str(error) + '>')
                    self.dismiss.wait(self.params['importer_retry_timeout'])
                    continue

                if len(checks['status']) > 0:
                    self.log.info('got %s checks' % len(checks['status']))

                try:
                    for status in checks['status']:

                        req = threadpool.WorkRequest(
                            self.job_start,
                            [ {
                                'check' : checks['checks'][status['chk_id']],
                                'group' : checks['groups'][status['grp_id']],
                                'object' : checks['objects'][status['obj_id']],
                                'status' : status}],
                            None,
                            request_id=status['status_id'],
                            callback=self.job_stop,
                            exc_callback=self.handle_exception
                        )
                        self.job_pool.queue_request(req, self.params['check_poll'])
                        self.log.info('Work request #%s added.' % req.request_id)
                except Queue.Full:
                    self.log.error("queue is full")
                    continue

            except Exception:
                self.log.error('caught unknown exception :')
                self.log.error(traceback.format_exc())
                continue

        self.log.info('dismissing workers')
        self.job_pool.dismiss_workers(int(self.params['max_parallel_checks']))

        # Do not join, takes time and results will not be written to database anyway
        self.log.info("plugin stopped")

    def create_new_job(self, _job):
        """ Dummy method. To be overridden in plugins. """

        raise BasePluginError('Plugin %s does not implement <create_new_job>' % self.name)

