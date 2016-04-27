#########
# Copyright (c) 2015 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

import getpass
import json
import os
import time

from cloudify.utils import (LocalCommandRunner,
                            setup_logger)
from cloudify import amqp_client
from cloudify.constants import (
    BROKER_PORT_NO_SSL,
    BROKER_PORT_SSL,
)

from cloudify_agent import VIRTUALENV
from cloudify_agent.api import utils
from cloudify_agent.api import exceptions
from cloudify_agent.api import defaults


class Daemon(object):

    """
    Base class for daemon implementations.
    Following is all the available common daemon keyword arguments. These
    will be available to any daemon without any configuration as instance
    attributes.

    ``internal_manager_host``:

        the manager's IP or hostname exposed to internal components (Required)

    ``user``:

        the user this daemon will run under. default to the current user.

    ``name``:

        the name to give the daemon. This name will be a unique identifier of
        the daemon. meaning you will not be able to create more daemons with
        that name until a delete operation has been performed. defaults to
        a unique name generated by cloudify.

    ``queue``:

        the queue this daemon will listen to. It is possible to create
        different workers with the same queue, however this is discouraged.
        to create more workers that process tasks from a given queue, use the
        'min_workers' and 'max_workers' keys. defaults to <name>-queue.

    ``host``:

        the ip address of the host the agent will be started on. this
        property is used only when the 'queue' or 'name' property are omitted,
        in order to retrieve the agent name and queue from the manager. in
        such case, this property must match the 'ip' runtime property given
        to the corresponding Compute node.

    ``deployment_id``:

        the deployment id this agent will be a part of. this
        property is used only when the 'queue' or 'name' property are omitted,
        in order to retrieve the agent name and queue from the manager.

    ``workdir``:

        working directory for runtime files (pid, log).
        defaults to the current working directory.

    ``broker_ip``:

        the ip address of the broker to connect to.
        defaults to the internal_manager_host value.

    ``broker_ssl_enabled``:

        Whether SSL is enabled for the broker.

    ``broker_ssl_cert``:

        The SSL public certificate for the broker, if SSL is enabled on the
        broker. This should be in PEM format and should be the string
        representation, including the 'BEGIN CERTIFICATE' header and footer.

    ``broker_user``

        the username for the broker connection
        defaults to 'guest'

    ``broker_pass``

        the password for the broker connection
        defaults to 'guest'

    ``rest_host``:

        the ip address/host name of the manager, running the
        REST service. (Required)

    ``rest_protocol``:

        the protocol to use in REST call. defaults to HTTP.

    ``rest_port``:

        the manager REST gateway port to connect to. defaults to 80.

    ``security_enabled``:

        True if REST security is enabled, False otherwise

    ``manager_username``:

        the username to use in REST call. No default.

    ``manager_password``:

        the password to use in REST call. No default.

    ``verify_certificate``:

        indicates if the verify the server's SSL certificate or not

    ``local_rest_cert_file``:

        A path to a local copy of the manager's SSL certificate, to be used
        for certificate verification if SSL is enabled.

    ``rest_ssl_cert_content``:
        The content of the REST SSL certificate, to be written to
        local_rest_cert_file

    ``min_workers``:

        the minimum number of worker processes this daemon will manage. all
        workers will listen on the same queue allowing for higher
        concurrency when preforming tasks. defaults to 0.

    ``max_workers``:

        the maximum number of worker processes this daemon will manage.
        as tasks keep coming in, the daemon will expand its worker pool to
        handle more tasks concurrently. However, as the name
        suggests, it will never exceed this number. allowing for the control
        of resource usage. defaults to 5.

    ``extra_env_path``:

        path to a file containing environment variables to be added to the
        daemon environment. the file should be in the format of
        multiple 'export A=B' lines for linux, ot 'set A=B' for windows.
        defaults to None.

    ``log_level``:

        log level of the daemon process itself. defaults to debug.

    ``log_file``:

        location of the daemon log file. defaults to <workdir>/<name>.log

    ``pid_file``:

        location of the daemon pid file. defaults to <workdir>/<name>.pid

    """

    # override this when adding implementations.
    PROCESS_MANAGEMENT = None

    # add specific mandatory parameters for different implementations.
    # they will be validated upon daemon creation
    MANDATORY_PARAMS = [
        'rest_host',
        'internal_manager_host'
    ]

    def __init__(self, logger=None, **params):

        """

        ####################################################################
        # When subclassing this, do not implement any logic inside the
        # constructor except for in-memory calculations and settings, as the
        # daemon may be instantiated many times for an existing agent. Also,
        # all daemon attributes must be JSON serializable, as daemons are
        # represented as dictionaries and stored as JSON files on Disk. If
        # you wish to have a non serializable attribute, mark it private by
        # naming it _<name>. Attributes starting with underscore will be
        # omitted when serializing the object.
        ####################################################################

        :param logger: a logger to be used to log various subsequent
        operations.
        :type logger: logging.Logger

        :param params: key-value pairs as stated above.
        :type params dict

        """

        # will be populated later on with runtime properties of the host
        # node instance this agent is dedicated for (if needed)
        self._runtime_properties = None

        # configure logger
        self._logger = logger or setup_logger(
            logger_name='cloudify_agent.api.pm.{0}'
            .format(self.PROCESS_MANAGEMENT))

        # save params
        self._params = params

        # configure command runner
        self._runner = LocalCommandRunner(logger=self._logger)

        # Mandatory parameters
        self.validate_mandatory()
        self.internal_manager_host = params['internal_manager_host']
        self.rest_host = params['rest_host']

        # Optional parameters
        self.validate_optional()
        self.name = params.get(
            'name') or self._get_name_from_manager()
        self.user = params.get('user') or getpass.getuser()
        self.broker_ip = params.get(
            'broker_ip') or self.rest_host
        self.broker_ssl_enabled = params.get('broker_ssl_enabled', False)
        self.broker_ssl_cert_content = params.get('broker_ssl_cert', '')
        self.broker_ssl_cert_path = params.get('broker_ssl_cert_path', '')
        # Port must be determined after SSL enabled has been set in order for
        # intelligent port selection to work properly
        self.broker_port = self._get_broker_port()
        self.broker_user = params.get('broker_user', 'guest')
        self.broker_pass = params.get('broker_pass', 'guest')
        self.host = params.get('host')
        self.deployment_id = params.get('deployment_id')
        self.rest_port = params.get(
            'rest_port') or defaults.REST_PORT
        self.rest_protocol = params.get(
            'rest_protocol') or defaults.REST_PROTOCOL
        self.security_enabled = params.get('security_enabled')
        self.manager_username = params.get('manager_username')
        self.manager_password = params.get('manager_password')
        self.verify_manager_certificate = params.get(
            'verify_manager_certificate') or \
            defaults.VERIFY_MANAGER_CERTIFICATE
        self.local_rest_cert_path = params.get('local_rest_cert_path', '')
        self.rest_cert_content = params.get('rest_ssl_cert_content')
        self.queue = params.get(
            'queue') or self._get_queue_from_manager()

        # This is not retrieved by param as an option any more as it then
        # introduces ambiguity over which values should be used if the
        # components of this differ from the passed in broker_user, pass, etc
        # These components need to be known for the _delete_amqp_queues
        # function.
        self.broker_url = defaults.BROKER_URL.format(
            host=self.broker_ip,
            port=self.broker_port,
            username=self.broker_user,
            password=self.broker_pass,
        )
        self.min_workers = params.get(
            'min_workers') or defaults.MIN_WORKERS
        self.max_workers = params.get(
            'max_workers') or defaults.MAX_WORKERS
        self.workdir = params.get(
            'workdir') or os.getcwd()
        self.extra_env_path = params.get('extra_env_path')
        self.log_level = params.get('log_level') or defaults.LOG_LEVEL
        self.log_file = params.get(
            'log_file') or os.path.join(self.workdir,
                                        '{0}.log'.format(self.name))
        self.pid_file = params.get(
            'pid_file') or os.path.join(self.workdir,
                                        '{0}.pid'.format(self.name))

        # create working directory if its missing
        if not os.path.exists(self.workdir):
            self._logger.debug('Creating directory: {0}'.format(self.workdir))
            os.makedirs(self.workdir)

        # save as attributes so that they will be persisted in the json files.
        # we will make use of these values when loading agents by name.
        self.process_management = self.PROCESS_MANAGEMENT
        self.virtualenv = VIRTUALENV

    def _get_celery_conf_path(self):
        return os.path.join(self.workdir, 'broker_config.json')

    def create_celery_conf(self):
        self._logger.info('Deploying celery configuration.')
        config = {
            'broker_ssl_enabled': self.broker_ssl_enabled,
            'broker_cert_path': self.broker_ssl_cert_path,
            'broker_username': self.broker_user,
            'broker_password': self.broker_pass,
            'broker_hostname': self.broker_ip,
        }
        with open(self._get_celery_conf_path(), 'w') as conf_handle:
            json.dump(config, conf_handle)

    def validate_mandatory(self):

        """
        Validates that all mandatory parameters are given.

        :raise DaemonMissingMandatoryPropertyError: in case one of the
        mandatory parameters is missing.
        """

        for param in self.MANDATORY_PARAMS:
            if param not in self._params:
                raise exceptions.DaemonMissingMandatoryPropertyError(param)

    def validate_optional(self):

        """
        Validates any optional parameters given to the daemon.

        :raise DaemonPropertiesError:
        in case one of the parameters is faulty.
        """

        self._validate_autoscale()
        self._validate_host()

    def _get_broker_port(self):
        """
        Determines the broker port if it has not been provided. Only intended
        to be called before self.broker_port has been set and after
        self.broker_ssl_cert has been set.
        """
        if self.broker_ssl_enabled:
            return BROKER_PORT_SSL
        else:
            return BROKER_PORT_NO_SSL

    def _is_agent_registered(self):
        celery_client = utils.get_celery_client(
            broker_url=self.broker_url,
            broker_ssl_enabled=self.broker_ssl_enabled,
            broker_ssl_cert_path=self.broker_ssl_cert_path)
        try:
            self._logger.debug('Retrieving daemon registered tasks')
            return utils.get_agent_registered(self.name, celery_client)
        finally:
            if celery_client:
                celery_client.close()

    ########################################################################
    # the following methods must be implemented by the sub-classes as they
    # may exhibit custom logic. usually this would be related to process
    # management specific configuration files.
    ########################################################################

    def delete(self, force=defaults.DAEMON_FORCE_DELETE):

        """
        Delete any resources created for the daemon in the 'configure' method.

        :param force: if the daemon is still running, stop it before
                      deleting it.
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def start_command(self):

        """
        Construct a command line for starting the daemon.
        (e.g sudo service <name> start)

        :return a one liner command to start the daemon process.
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def stop_command(self):

        """
        Construct a command line for stopping the daemon.
        (e.g sudo service <name> stop)

        :return a one liner command to stop the daemon process.
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def status(self):

        """
        Query the daemon status, This method can be usually implemented
        by simply running the status command. However, this is not always
        the case, as different commands and process management tools may
        behave differently.

        :return: True if the service is running, False otherwise
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def create_script(self):
        raise NotImplementedError('Must be implemented by a subclass')

    def create_config(self):
        raise NotImplementedError('Must be implemented by a subclass')

    ########################################################################
    # the following methods is the common logic that would apply to any
    # process management implementation.
    ########################################################################

    def create(self):

        """
        Creates the agent. This method may be served as a hook to some custom
        logic that needs to be implemented after the instance
        was instantiated.

        """
        self._logger.debug('Daemon created')

    def configure(self):

        """
        Creates any necessary resources for the daemon. After this method
        was completed successfully, it should be possible to start the daemon
        by running the command returned by the `start_command` method.

        """
        self._logger.debug('Creating daemon script: {0}'
                           .format(self.script_path))
        self.create_script()
        self._logger.debug('Creating daemon conf file: {0}'
                           .format(self.config_path))
        self.create_config()
        self.create_celery_conf()

    def start(self,
              interval=defaults.START_INTERVAL,
              timeout=defaults.START_TIMEOUT,
              delete_amqp_queue=defaults.DELETE_AMQP_QUEUE_BEFORE_START):

        """
        Starts the daemon process.

        :param interval: the interval in seconds to sleep when waiting for
                         the daemon to be ready.
        :param timeout: the timeout in seconds to wait for the daemon to be
                        ready.
        :param delete_amqp_queue: delete any queues with the name of the
                                  current daemon queue in the broker.

        :raise DaemonStartupTimeout: in case the agent failed to start in the
        given amount of time.
        :raise DaemonException: in case an error happened during the agent
        startup.

        """

        if delete_amqp_queue:
            self._logger.debug('Deleting AMQP queues')
            self._delete_amqp_queues()
        start_command = self.start_command()
        self._logger.info('Starting daemon with command: {0}'
                          .format(start_command))
        self._runner.run(start_command)
        end_time = time.time() + timeout
        while time.time() < end_time:
            self._logger.debug('Querying daemon {0} registered tasks'.format(
                self.name))
            if self._is_agent_registered():
                # make sure the status command recognizes the daemon is up
                status = self.status()
                if status:
                    self._logger.debug('Daemon {0} has started'
                                       .format(self.name))
                    return
            self._logger.debug('Daemon {0} has not started yet. '
                               'Sleeping for {1} seconds...'
                               .format(self.name, interval))
            time.sleep(interval)
        self._logger.debug('Verifying there were no un-handled '
                           'exception during startup')
        self._verify_no_celery_error()
        raise exceptions.DaemonStartupTimeout(timeout, self.name)

    def stop(self,
             interval=defaults.STOP_INTERVAL,
             timeout=defaults.STOP_TIMEOUT):

        """
        Stops the daemon process.

        :param interval: the interval in seconds to sleep when waiting for
                         the daemon to stop.
        :param timeout: the timeout in seconds to wait for the daemon to stop.

        :raise DaemonShutdownTimeout: in case the agent failed to be stopped
        in the given amount of time.
        :raise DaemonException: in case an error happened during the agent
        shutdown.

        """

        stop_command = self.stop_command()
        self._logger.info('Stopping daemon with command: {0}'
                          .format(stop_command))
        self._runner.run(stop_command)
        end_time = time.time() + timeout
        while time.time() < end_time:
            self._logger.debug('Querying daemon {0} registered tasks'.format(
                self.name))
            # check the process has shutdown
            if not self._is_agent_registered():
                # make sure the status command also recognizes the
                # daemon is down
                status = self.status()
                if not status:
                    self._logger.debug('Daemon {0} has shutdown'
                                       .format(self.name, interval))
                    self._logger.debug('Deleting AMQP queues')
                    self._delete_amqp_queues()
                    return
            self._logger.debug('Daemon {0} is still running. '
                               'Sleeping for {1} seconds...'
                               .format(self.name, interval))
            time.sleep(interval)
        self._logger.debug('Verifying there were no un-handled '
                           'exception during startup')
        self._verify_no_celery_error()
        raise exceptions.DaemonShutdownTimeout(timeout, self.name)

    def restart(self,
                start_timeout=defaults.START_TIMEOUT,
                start_interval=defaults.START_INTERVAL,
                stop_timeout=defaults.STOP_TIMEOUT,
                stop_interval=defaults.STOP_INTERVAL):

        """
        Restart the daemon process.

        :param start_interval: the interval in seconds to sleep when waiting
                               for the daemon to start.
        :param start_timeout: The timeout in seconds to wait for the daemon
                              to start.
        :param stop_interval: the interval in seconds to sleep when waiting
                              for the daemon to stop.
        :param stop_timeout: the timeout in seconds to wait for the daemon
                             to stop.

        :raise DaemonStartupTimeout: in case the agent failed to start in the
        given amount of time.
        :raise DaemonShutdownTimeout: in case the agent failed to be stopped
        in the given amount of time.
        :raise DaemonException: in case an error happened during startup or
        shutdown

        """

        self.stop(timeout=stop_timeout,
                  interval=stop_interval)
        self.start(timeout=start_timeout,
                   interval=start_interval)

    def before_self_stop(self):

        """
        Optional method that can be implemented by subclasses. This method
        will be called before operations that involve the daemon stopping
        itself and therefore, can be used for cleanup purposes.
        """
        pass

    def get_logfile(self):

        """
        Injects worker_id placeholder into logfile. Celery library will replace
        this placeholder with worker id. It is used to make sure that there is
        at most one process writing to a specific log file.

        """

        path, extension = os.path.splitext(self.log_file)
        return '{0}{1}{2}'.format(path,
                                  self.get_worker_id_placeholder(),
                                  extension)

    def get_worker_id_placeholder(self):

        """
        Placeholder suitable for linux systems.

        """

        return '%I'

    def _verify_no_celery_error(self):

        error_dump_path = os.path.join(
            utils.internal.get_storage_directory(self.user),
            '{0}.err'.format(self.name))

        # this means the celery worker had an uncaught
        # exception and it wrote its content
        # to the file above because of our custom exception
        # handler (see app.py)
        if os.path.exists(error_dump_path):
            with open(error_dump_path) as f:
                error = f.read()
            os.remove(error_dump_path)
            raise exceptions.DaemonError(error)

    def _delete_amqp_queues(self):
        client = amqp_client.create_client(
            amqp_host=self.broker_ip,
            amqp_user=self.broker_user,
            amqp_pass=self.broker_pass,
            ssl_enabled=self.broker_ssl_enabled,
            ssl_cert_path=self.broker_ssl_cert_path,
        )

        try:
            channel = client.connection.channel()
            self._logger.debug('Deleting queue: {0}'.format(self.queue))

            channel.queue_delete(self.queue)
            pid_box_queue = 'celery@{0}.celery.pidbox'.format(self.name)
            self._logger.debug('Deleting queue: {0}'.format(pid_box_queue))
            channel.queue_delete(pid_box_queue)
        finally:
            try:
                client.close()
            except Exception as e:
                self._logger.warning('Failed closing amqp client: {0}'
                                     .format(e))

    def _validate_autoscale(self):
        min_workers = self._params.get('min_workers')
        max_workers = self._params.get('max_workers')
        if min_workers:
            if not str(min_workers).isdigit():
                raise exceptions.DaemonPropertiesError(
                    'min_workers is supposed to be a number '
                    'but is: {0}'
                    .format(min_workers)
                )
            min_workers = int(min_workers)
        if max_workers:
            if not str(max_workers).isdigit():
                raise exceptions.DaemonPropertiesError(
                    'max_workers is supposed to be a number '
                    'but is: {0}'
                    .format(max_workers)
                )
            max_workers = int(max_workers)
        if min_workers and max_workers:
            if min_workers > max_workers:
                raise exceptions.DaemonPropertiesError(
                    'min_workers cannot be greater than max_workers '
                    '[min_workers={0}, max_workers={1}]'
                    .format(min_workers, max_workers))

    def _validate_host(self):
        queue = self._params.get('queue')
        host = self._params.get('host')
        if not queue and not host:
            raise exceptions.DaemonPropertiesError(
                'host must be supplied when queue is omitted'
            )

    def _validate_deployment_id(self):
        queue = self._params.get('queue')
        host = self._params.get('deployment_id')
        if not queue and not host:
            raise exceptions.DaemonPropertiesError(
                'deployment_id must be supplied when queue is omitted'
            )

    def _get_name_from_manager(self):
        if self._runtime_properties is None:
            self._get_runtime_properties()
        return self._runtime_properties['cloudify_agent']['name']

    def _get_queue_from_manager(self):
        if self._runtime_properties is None:
            self._get_runtime_properties()
        return self._runtime_properties['cloudify_agent']['queue']

    def _get_runtime_properties(self):
        client = utils.get_rest_client(
            security_enabled=self.security_enabled,
            rest_host=self.rest_host,
            rest_protocol=self.rest_protocol,
            rest_port=self.rest_port,
            cloudify_username=self.manager_username,
            cloudify_password=self.manager_password,
            verify_manager_certificate=self.verify_manager_certificate,
            ssl_cert_path=self.local_rest_cert_path
        )
        node_instances = client.node_instances.list(
            deployment_id=self.deployment_id)

        def match_ip(node_instance):
            host_id = node_instance.host_id
            if host_id == node_instance.id:
                # compute node instance
                return self.host == node_instance.runtime_properties['ip']
            return False

        matched = filter(match_ip, node_instances)

        if len(matched) > 1:
            raise exceptions.DaemonConfigurationError(
                'Found multiple node instances with ip {0}: {1}'.format(
                    self.host, ','.join(matched))
            )

        if len(matched) == 0:
            raise exceptions.DaemonConfigurationError(
                'No node instances with ip {0} were found'.format(self.host)
            )
        self._runtime_properties = matched[0].runtime_propreties

    def _list_plugin_files(self, plugin_name):

        """
        Retrieves python files related to the plugin.
        __init__ file are filtered out.

        :param plugin_name: The plugin name.

        :return: A list of file paths.
        :rtype: list of str
        """

        module_paths = []
        runner = LocalCommandRunner(self._logger)

        files = runner.run(
            '{0} show -f {1}'
            .format(utils.get_pip_path(), plugin_name)
        ).std_out.splitlines()
        for module in files:
            if self._is_valid_module(module):
                # the file paths are relative to the
                # package __init__.py file.
                prefix = '../' if os.name == 'posix' else '..\\'
                module_paths.append(
                    module.replace(prefix, '')
                    .replace(os.sep, '.').replace('.py', '').strip())
        return module_paths

    @staticmethod
    def _is_valid_module(module):
        if not module.endswith('py'):
            return False
        if '__init__' in module:
            return False
        if '-' in os.path.basename(module):
            return False
        return True


class CronRespawnDaemon(Daemon):

    """
    This Mixin exposes capabilities for adding a cron job that re-spawns
    the daemon in case of a failure.

    Usage:

        run(self.create_enable_cron_script)
        run(self.create_disable_cron_script)

    Following are all possible custom key-word arguments
    (in addition to the ones available in the base daemon)

    ``cron_respawn``

        pass True to enable cron detection. False otherwise

    ``cron_respawn_delay``

        the amount of minutes to wait before each cron invocation.

    """

    def __init__(self, logger=None, **params):
        super(CronRespawnDaemon, self).__init__(logger, **params)
        self.cron_respawn_delay = params.get('cron_respawn_delay', 1)
        self.cron_respawn = params.get('cron_respawn', False)

    def status_command(self):

        """
        Construct a command line for querying the status of the daemon.
        (e.g sudo service <name> status)
        the command execution should result in a zero return code if the
        service is running, and a non-zero return code otherwise.

        :return a one liner command to start the daemon process.
        :rtype: str
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def create_enable_cron_script(self):

        enable_cron_script = os.path.join(
            self.workdir, '{0}-enable-cron.sh'.format(self.name))

        cron_respawn_path = os.path.join(
            self.workdir, '{0}-respawn.sh'.format(self.name))

        self._logger.debug('Rendering respawn script from template')
        utils.render_template_to_file(
            template_path='respawn.sh.template',
            file_path=cron_respawn_path,
            start_command=self.start_command(),
            status_command=self.status_command()
        )
        self._runner.run('chmod +x {0}'.format(cron_respawn_path))
        self._logger.debug('Rendering enable cron script from template')
        utils.render_template_to_file(
            template_path='crontab/enable.sh.template',
            file_path=enable_cron_script,
            cron_respawn_delay=self.cron_respawn_delay,
            cron_respawn_path=cron_respawn_path,
            user=self.user,
            workdir=self.workdir,
            name=self.name
        )
        self._runner.run('chmod +x {0}'.format(enable_cron_script))
        return enable_cron_script

    def create_disable_cron_script(self):

        disable_cron_script = os.path.join(
            self.workdir, '{0}-disable-cron.sh'.format(self.name))

        self._logger.debug('Rendering disable cron script from template')
        utils.render_template_to_file(
            template_path='crontab/disable.sh.template',
            file_path=disable_cron_script,
            name=self.name,
            user=self.user,
            workdir=self.workdir
        )
        self._runner.run('chmod +x {0}'.format(disable_cron_script))
        return disable_cron_script
