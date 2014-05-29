"""
x84net message network client/server
"""

import web

""" server queues and locking mechanism """
class MessageNetworkServer():
    iqueue = None
    oqueue = None
    lock = None

""" api endpoint """
class messages():
    def GET(self, network, last):
        import json
        import Queue

        if len(last) <= 0:
            last = 0
        else:
            last = int(last)

        if not 'HTTP_AUTH_X84NET' in web.ctx.env.keys():
            raise web.HTTPError('401 Unauthorized', {}, 'Unauthorized')

        data = {
            'auth': web.ctx.env['HTTP_AUTH_X84NET']
            , 'network': network
            , 'action': 'pull'
            , 'last': last
        }

        MessageNetworkServer.lock.acquire()
        MessageNetworkServer.iqueue.put(data)

        try:
            response = MessageNetworkServer.oqueue.get(True, 60)
        except Queue.Empty:
            response = {u'response': False, u'message': u'No response'}

        MessageNetworkServer.lock.release()

        try:
            return json.dumps(response)
        except:
            return response

    def PUT(self, network, null):
        import json
        import Queue

        if not 'HTTP_AUTH_X84NET' in web.ctx.env.keys():
            raise web.HTTPError('401 Unauthorized', {}, 'Unauthorized')

        webdata = web.input()

        data = {
            'auth': web.ctx.env['HTTP_AUTH_X84NET']
            , 'network': network
            , 'action': 'push'
            , 'message': json.loads(webdata.message)
        }

        MessageNetworkServer.lock.acquire()
        MessageNetworkServer.iqueue.put(data)

        try:
            response = MessageNetworkServer.oqueue.get(True, 60)
        except Queue.Empty:
            response = {u'response': False, u'message': u'No response'}

        MessageNetworkServer.lock.release()

        try:
            return json.dumps(response)
        except:
            return response

""" fire up the server """
def start():
    from x84.bbs import ini
    from web.wsgiserver import CherryPyWSGIServer
    from threading import Lock
    from multiprocessing import Queue

    CherryPyWSGIServer.ssl_certificate = ini.CFG.get('web', 'cert')
    CherryPyWSGIServer.ssl_private_key = ini.CFG.get('web', 'key')

    if ini.CFG.has_option('web', 'chain'):
        CherryPyWSGIServer.ssl_certificate_chain = ini.CFG.get('web', 'chain')

    urls = (
        '/messages/([^/]+)/([^/]*)/?', 'messages'
        )

    app = web.application(urls, globals())
    web.httpserver.runsimple(app.wsgifunc(), (ini.CFG.get('web', 'addr'), ini.CFG.getint('web', 'port')))

""" functions for processing the request within x84 """

""" helper method for logging and returning errors """
def server_error(logger, queue, logtext, message=None):
    if message is None:
        message = logtext
    logger.error(logtext)
    queue.put({u'response': False, u'message': message})

""" server request handling process """
def main():
    from x84.bbs import ini, msgbase, DBProxy, getsession, echo, getterminal
    from x84.bbs.msgbase import to_utctime, to_localtime, Msg
    import hashlib
    import time
    import logging
    import json
    import Queue

    session = getsession()
    logger = logging.getLogger()
    term = getterminal()

    while True:
        try:
            data = MessageNetworkServer.iqueue.get(True, 10)
        except Queue.Empty:
            continue

        queue = MessageNetworkServer.oqueue

        if 'network' not in data.keys():
            server_error(logger, queue, u'Network not specified')
            continue

        if 'action' not in data.keys():
            server_error(logger, queue, u'Action not specified')
            continue

        if 'auth' not in data.keys():
            server_error(logger, queue, u'Auth token missing')
            continue

        auth = data['auth'].split('|')

        if len(auth) != 3:
            server_error(logger, queue, u'Improper token')
            continue

        board_id = int(auth[0])
        token = auth[1]
        when = int(auth[2])
        now = int(time.time())
        netcfg = 'msgnet_%s' % data['network']
        logger.info(u"client %d connecting for '%s' %s" % (board_id, data['network'], data['action']))

        if not ini.CFG.has_option(netcfg, 'keys_db_name'):
            server_error(logger, queue, u'No keys database config for this network', u'Server error')
            continue

        keysdb = DBProxy(ini.CFG.get(netcfg, 'keys_db_name'))

        if str(board_id) not in keysdb.keys():
            server_error(logger, queue, u'No such key for this network')
            continue

        board_key = keysdb[str(board_id)]

        if when > now or now - when > 15:
            server_error(logger, queue, u'Expired token')
            continue

        if token != hashlib.sha256('%s%d' % (board_key, when)).hexdigest():
            server_error(logger, queue, u'Invalid token')
            continue

        if not ini.CFG.has_option(netcfg, 'source_db_name'):
            server_error(logger, queue, u'Source DB not configured', u'Server error')
            continue

        if not ini.CFG.has_option(netcfg, 'trans_db_name'):
            server_error(logger, queue, u'Translation DB not configured', u'Server error')
            continue

        tagdb = DBProxy(msgbase.TAGDB)
        msgdb = DBProxy(msgbase.MSGDB)
        sourcedb = DBProxy(ini.CFG.get(netcfg, 'source_db_name'))
        transdb = DBProxy(ini.CFG.get(netcfg, 'trans_db_name'))

        if data['action'] == 'pull':

            """ client is requesting to pull messages """

            if data['network'] not in tagdb.keys():
                queue.put({u'response': True, u'messages': []})
                continue

            msgs = list()
            last = None

            if 'last' in data.keys():
                last = int(data['last'])

            count = 0

            for m in tagdb[data['network']]:
                if count == 20:
                    break

                if last != None and int(m) <= last:
                    continue

                count += 1
                msg = msgdb[m]

                # don't pull messages that the client posted
                if sourcedb.has_key(msg.idx) and sourcedb[msg.idx] == int(board_id):
                    continue

                pushmsg = {
                    u'id': msg.idx
                    , u'author': msg.author
                    , u'recipient': msg.recipient
                    , u'parent': msg.parent
                    , u'subject': msg.subject
                    , u'tags': [tag for tag in msg.tags if tag != data['network']]
                    , u'ctime': to_utctime(msg.ctime)
                    , u'body': msg.body
                }
                msgs.append(pushmsg)

            queue.put({u'response': True, u'messages': msgs})
        elif data['action'] == 'push':

            """ client is requesting to push messages """

            if 'message' not in data.keys():
                server_error(logger, queue, u'No message')
                continue

            pullmsg = data['message']

            for k in [u'author', u'recipient', u'subject', u'parent', u'tags', u'ctime', u'body']:
                if k not in pullmsg.keys():
                    server_error(logger, queue, u'Missing %s' % k)
                    continue

            msg = Msg()
            msg.author = pullmsg['author']
            msg.recipient = pullmsg['recipient']
            msg.subject = pullmsg['subject']
            msg.parent = pullmsg['parent']
            msg.tags = set(pullmsg['tags'] + [data['network']])
            msg.body = pullmsg['body']

            try:
                msg.save(noqueue=True, ctime=to_localtime(pullmsg['ctime'].partition('.')[0]))
            except Exception, e:
                server_error('Error saving message: %s' % str(e))
                continue

            sourcedb.acquire()
            transdb.acquire()
            sourcedb[msg.idx] = board_id
            transdb[msg.idx] = msg.idx
            sourcedb.release()
            transdb.release()
            queue.put({u'response': True, u'id': msg.idx})
