#! /usr/bin/env python
# coding=utf-8

import os
import os.path
import re
import sys
import time
import couchdb
import logging as logger
import cgi
import BaseHTTPServer
import SocketServer
import formatter



table_name = 'requests'


class MyHandler(BaseHTTPServer.BaseHTTPRequestHandler):

    def __get_log(self):
        idx = re.search('/(.+?)\.log', self.path).group(1)
        couch = couchdb.Server()
        db = couch[table_name]
        if idx not in db:
            return
        doc = db[idx]
        data = db.get_attachment(doc, 'log')
        if data is None:
            data = "Log is not yet created"
        else:
            data = data.read()

        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        buf = '<html><meta charset="UTF-8"><body><code>' + data.replace('\n', '<br/>') + '</code></body></html>'
        self.wfile.write(buf)


    def __construct_table(self, title, results):
        logger.debug('__construct_table with {0} and {1}'.format(title, results))
        if results is None:
            results = {}
        table = '<tr>'
        rowspan = len(results)
        table += '<th rowspan={0}>{1}</th>'.format(rowspan, title)
        for k, v in results.items():
            logger.debug('output value {0}'.format(v))
            if not isinstance(v, dict):
                table += '<th>{0}</th><td>{1}</td></tr>'.format(k, v)
            elif 'name' in v and 'content' in v: # v is file
                table += '<th>{0}</th><td>{1}</td></tr>'.format(k, v['name'])
            elif 'head' in v and 'body' in v: # v is table
                res_table = '<table>'
                res_table += '<tr>' + ''.join(['<th>{0}</th>'.format(tk) for tk in v['head']]) + '</tr>'
                for row in v['body']:
                    res_table += '<tr>' + ''.join(['<td>{0}</td>'.format(rv) for rv in row]) + '</tr>'
                res_table += '</table>'
                table += '<th>{0}</th><td>{1}</td></tr>'.format(k, res_table)
            table += '<tr>'
        table += '</tr>'
        return table


    def __construct_result_table(self):
        couch = couchdb.Server()
        db = couch[table_name]
        docs = [db[idx] for idx in db]

        table = ''

        # Table cells
        for req in sorted(docs, key=lambda d: d['timestampt'], reverse=True):
            table += '<table border=1>'
            
            for task_name, task_opts in req['tasks'].items():
                table += self.__construct_table(task_name, task_opts['args'])
                table += self.__construct_table('Results', task_opts.get('result', dict()))

            table += self.__construct_table('System', {'status': req['status'], 'host': req['host']})
            table += '</table><br/>'
        return table


    def __construct_input_form(self):
        couch = couchdb.Server()
        if 'tasks' not in couch:
            all_task_configs = {}
        else:
            db = couch['tasks']
            doc = db['config']
            conf_names = doc['names']
            conf_opts  = doc['opts']
            all_task_configs = { tdir: conf_opts[tdir] for tdir in conf_names }

        form = '<h2>MyForm</h2><form action=/ method=POST ENCTYPE=multipart/form-data>'

        # construct tasks form
        for task_name, task in all_task_configs.items():
            title = task.get('title', task_name)
            form += '<fieldset><legend>' + title + '</legend>'
            form += '<input type="hidden" name="{0}.version" value="{1}" />'.format(task_name, task['version'])

            for param in task['args']: #  {"name": "some_field", "type": "text", "title": "User Name"}
                name = param['name']
                key = task_name + '.' + name
                tkey = task_name + '.' + name + '.type'
                title = param.get('title', name)
                input_type = param['type'] if param['type'] != 'bool' else 'checkbox'
                required = 'required' if 'required' in param and param['required'] else ''

                form += '<fieldset><legend>{0}</legend>'.format(title)

                if param['type'] in ['bool', 'text', 'file']:
                    form += '<input type="{0}" name="{1}" "{2}" />'.format(input_type, key, required)
                    form += '<input type="hidden" name="{0}" value="{1}" />'.format(tkey, param['type'])

                elif param['type'] in ['radio', 'checkbox']:
                    if 'values' not in param:
                        logger.warning('values are missed for param {0}'.format(name))
                    else:
                        for value in param['values']:
                            form += '<input type={0} name={1} value={2} {3} /> {4} <br/>'.format(input_type, key, value, required, value)
                        form += '<input type="hidden" name="{0}" value="{1}" />'.format(tkey, param['type'])
                else:
                    logger.warning('incorrect param {0} type {1}'.format(name, param['type']))

                form += '</fieldset>'

            form += '</fieldset>'

        form += '<input type=submit value="Upload patch">'
        form += '</form>'
        return form


    def get_message(self, db_answer=None):
        return message


    def under_construction(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        buf = '<html><meta charset="UTF-8"><body><h1>UNDER CONSTRUCTION</h1></body></html>'
        self.wfile.write(buf)


    def __get_form(self):
        form = '<html><head><title>DTS</title><meta charset="UTF-8">\
                <link rel = "stylesheet" type = "text/css" href = "style.css">\
                </head><body><table><tr><td style="padding:10px;vertical-align:top;">'

        form += self.__construct_input_form()

        form += '</td><td><div class=form>'

        form += self.__construct_result_table()

        form += '</div></td></tr></table>'
        form += '</body></html>'

        self.send_response(200)
        self.end_headers()
        self.wfile.write(form.encode('utf-8'))


    def do_GET(self):
        if self.path.endswith('.log'):
            self.__get_log()
        else:
            self.__get_form()


    def do_POST(self):
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': self.headers['Content-Type'], })
        tasks_args = {}

        for k in form:
            if k.endswith('.version'):
                tkey_list = k.split('.')
                task_name = tkey_list[0]
                task_version = form[k].value
                tasks_args.setdefault(task_name, dict()).setdefault('version', task_version)
            elif k.endswith('.type'):
                tkey_list = k.split('.')
                task_name = tkey_list[0]
                param_name = tkey_list[1]
                param_type = form[k].value

                logger.debug('checking field {0}'.format(k))
                
                k_value = k.replace('.type', '')
                param_val = form[k_value] if k_value in form else None

                logger.debug('field {0} values are {1}'.format(k_value, 'found' if param_val is not None else 'not-found'))
                logger.debug('field {0}:\n{1}'.format(k_value, param_val))

                task_args = tasks_args.setdefault(task_name, dict()).setdefault('args', dict())

                if param_type == 'bool':
                    task_args[param_name] = param_val is not None
                elif param_type in ['text', 'radio']:
                    task_args[param_name] = None if param_val is None or param_val.value == '' else param_val.value
                elif param_type == 'checkbox':
                    task_args[param_name] = list()
                    if param_val is not None:
                        if isinstance(param_val, list):
                            for v in param_val:
                                task_args[param_name].append(v.value)
                        else:
                            task_args[param_name].append(param_val.value)
                    else:
                        task_args[param_name] = None
                elif param_type == 'file':
                    if param_val is None or param_val.filename == '':
                        task_args[param_name] = None
                    else:
                        filename = os.path.basename(param_val.filename)
                        buf = param_val.file.read()
                        task_args[param_name] = {'name': filename, 'content': buf}
                else:
                    logger.warning('incorrect param type {0} in request'.format(param_type))

        self.send_response(200)
        self.end_headers()

        self.wfile.write('<html><head><title>Upload</title></head><body>\
                         Success\
                         <FORM><INPUT Type="button" VALUE="Back" onClick="history.go(-1);return true;"></FORM>\
                         </body></html>')

        doc = {'version': 1,
               'timestampt': time.time(),
               'host': None,
               'status': 'Waiting',
               'tasks': tasks_args}

        couch = couchdb.Server()
        db = couch[table_name]
        db.save(doc)


class ForkingHTTPServer(SocketServer.ForkingMixIn, BaseHTTPServer.HTTPServer):
    def finish_request(self, request, client_address):
        request.settimeout(30)
        # "super" can not be used because BaseServer is not created from object
        BaseHTTPServer.HTTPServer.finish_request(self, request, client_address)


if __name__ == '__main__':
    logger.basicConfig(level=logger.DEBUG)
    handler_class = MyHandler
    try:
        couch = couchdb.Server()
        if table_name not in couch:
            couch.create(table_name)

        port = 8080
        host = "balboa"
        url = "http://{0}:{1}/".format(host, port)
        print "Ask user to visit this URL:\n\t%s" % url
        srvr = ForkingHTTPServer(('', port), handler_class)
        srvr.serve_forever()  # serve_forever
    except KeyboardInterrupt:
        pass

