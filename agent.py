#! /usr/bin/env python
# coding=utf-8

import os
import os.path
import sys
import time
import json
import shutil
import socket
import logging as logger
from multiprocessing import Process, Queue
import couchdb
import subprocess



class Task:
    def __init__(self, name, args, conf, outdir, outlog):
        self.__args = args
        self.__log = log
        self.__name = name
        self.__proc = None
        self.__is_finished = False
        self.__results = None
        self.__refs = conf.get('refs', dict())

        self.__check_version(conf['version'], name)
        sub_cls = getattr(__import__(name, fromlist=['SubTask']), 'SubTask')
        self.__sub_task = sub_cls(outdir, outlog)


    def __check_version(self, version, name):
        global script_path
        task_config = os.path.join(script_path, 'tasks', name, 'config.json')
        if not os.path.isfile(task_config):
            raise ValueError, "No config file for task {0}".format(name)
        with open(task_config) as f:
            task_config = json.load(f)
        if int(task_config['version']) < int(version):
            raise ValueError, "Version {0} for task {1} is too old, task must be updated".format(version, name)


    def is_initialized(self):
        return self.__sub_task.is_initialized()


    def initialize(self):
        self.__sub_task.initialize()


    def is_alive(self):
        return self.__proc is not None and self.__proc.is_alive()


    def run(self):
        self.__q = Queue()
        self.__proc = Process(target=self.__sub_task.run, args=(self.__q, self.__args))
        self.__proc.start()


    def is_finished(self):
        if self.__is_finished:
            return True
        if self.__proc is not None and not self.__proc.is_alive():
            self.__proc.join()
            self.__is_finished = True
            self.__results = self.__q.get()
        return self.__is_finished


    def collect_argrefs(self, tasks):
        for k, v in self.__refs: # TODO maybe better store task config and use conf['refs']
            ref_task_name, ref_task_retarg = v.split('.')
            ref_task = tasks[ref_task_name]
            self.__args[k] = ref_task.get_result()[ref_task_retarg] # TODO raise missing key


    def get_result(self):
        return self.__results


    def get_name(self):
        return self.__name


    def get_log(self):
        return self.__log


class Req:
    def __init__(self, idx, doc, couch):
        self.__idx = idx
        self.__tasks = []
        self.__name2task = {}
        self.__running_task = None
        self.__finished = False

        db_configs = couch['configs']

        global script_path

        for task_name, task_opts in doc['tasks'].items():
            output_dir = os.path.join(script_path, 'results', doc['_id'], task_name)
            if not os.path.isdir(output_dir):
                os.makedirs(output_dir)
            output_log = output_dir + '.log'
            open(output_log, 'w').close() # recreate log for a task

            task_config = db_configs[task_name]

            T = Task(task_name, task_opts['args'], task_config, output_dir, output_log)
            logger.debug('Adding new task {0} with args {1}'.format(task_name, task_opts['args']))
            self.__tasks.append(T)
            self.__name2task[task_name] = T


    def idx(self):
        return self.__idx


    def probe(self, doc, db):
        # prepare correct currently running task
        logger.debug('probing with {0} {1}'.format(self.__tasks, self.__running_task))
        if self.__running_task is None:
            if len(self.__tasks) == 0:
                self.__finished = True
                return
            else:
                self.__running_task = self.__tasks[0]
                del self.__tasks[0]

        logger.debug('running task is {0}'.format(self.__running_task))

        if not self.__running_task.is_initialized():
            self.__running_task.initialize()
            return

        if self.__running_task.is_alive():
            logger.debug('running task is {0} is alive'.format(self.__running_task))
            task_log_buf = open(self.__running_task.get_log()).read()
            db.put_attachment(doc, task_log_buf, 'log')
        elif self.__running_task.is_finished(): # task is finished
            logger.debug('running task is {0} is finished'.format(self.__running_task))
            self.__dump_results(self.__running_task, doc, db)
            self.__running_task = None
        else: # start & init task
            self.__running_task.collect_argrefs(self.__name2task)
            self.__running_task.run()


    def finished(self):
        return self.__finished


    def kill(self):
        pass # TODO


    def __dump_results(self, task, doc, db):
        doc['tasks'][task.get_name()]['result'] = task.get_result()
        db.save(doc)
        task_log_buf = open(task.get_log()).read()
        db.put_attachment(doc, task_log_buf, 'log')


def __idx_lock(doc, db):
    doc['status'] = 'Processed'
    doc['host'] = socket.gethostname()
    db.save(doc)


def __idx_unlock(doc, db):
    doc['status'] = 'Done'
    doc['host'] = socket.gethostname()
    db.save(doc)


def __idx_locked(doc):
    return doc['status'] != 'Waiting'


def load_and_update_tasks(couch):
    if 'tasks' not in couch:
        couch.create('tasks')
    db = couch['tasks']

    if 'dirs' not in db:
        db['dirs'] = {'names': []}
    if 'configs' not in db:
        db['configs'] = {}

    doc_dirs = db['dirs']
    doc_configs = db['configs']

    # load task configs
    global script_path
    tasks_dir = os.path.join(script_path, 'tasks')
    logger.debug('Looking up for task modules in ' + tasks_dir)
    for task_sdir in os.listdir(tasks_dir):
        task_dir = os.path.join(tasks_dir, task_sdir)
        logger.debug('pending {0}'.format(task_dir))
        if not os.path.isdir(task_dir):
            continue

        task_config_fname = os.path.join(task_dir, 'config.json')
        if not os.path.isfile(task_config_fname):
            logger.warning('skipping task {0} becase config.json is missed'.format(task_dir))
            continue

        with open(task_config_fname) as f:
            try:
                task_config = json.load(f)
            except ValueError, e:
                logger.warning('task {0} config loading failed: {1}'.format(task_dir, e))
                continue

        logger.debug('task {0} config is loaded: {1}'.format(task_dir, task_config))

        if task_sdir not in doc_dirs['names']:
            doc_dirs['names'].append(task_sdir)
        else:
            actual_version = int(doc_configs[task_sdir]['version'])
            installed_version = int(task_config['version'])
            if installed_version < actual_version: # update installed task
                task_config = doc_configs[task_sdir]
                update_task(task_dir, task_config)

        doc_configs[task_sdir] = task_config

    db[doc_dirs.id] = doc_dirs
    db[doc_configs.id] = doc_configs


def update_task(task_dir, task_config):
    interpr = task_config['init']['interpreter']
    command = task_config['init']['command']
    retcode = subprocess.call([interpr, '-c', command])


def go():

    req = None

    while True:
        time.sleep(1)

        couch = couchdb.Server()
        db = couch['requests']

        logger.debug('new step')

        # process already started request
        if req is not None:
            logger.debug('req is {0}'.format(req))
            try:
                doc = db[req.idx()]
            except couchdb.http.ResourceNotFound:
                continue # TODO

            if req.finished(): # TODO dump overall status
                logger.debug('req is finished')
                req = None
                __idx_unlock(doc, db)
            elif doc['status'] == 'Kill':
                req.kill()
                req = None
            else:
                req.probe(doc, db)

        else: # select first non-locked request
            load_and_update_tasks(couch)
            for idx in db:
                doc = db[idx] # TODO exception
                if not __idx_locked(doc):
                    try:
                        req = Req(idx, doc, couch)
                    except ValueError, e:
                        req = None
                        logger.warning('Cannot create new reqest because of {0}'.format(e))
                    else:
                        __idx_lock(doc, db)
                    break



if __name__ == '__main__':
    logger.basicConfig(level=logger.DEBUG)

    script_path = os.path.dirname(os.path.realpath(__file__))
    sys.path.append(os.path.join(script_path, 'tasks'))

    try:
        go()
    except KeyboardInterrupt:
        pass

