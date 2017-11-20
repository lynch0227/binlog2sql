#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import os
import sys
import traceback

import pymysql
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.event import QueryEvent, RotateEvent, FormatDescriptionEvent
from pymysqlreplication.row_event import (
    WriteRowsEvent,
    UpdateRowsEvent,
    DeleteRowsEvent,
)

from binlog2sql_util import command_line_args, concat_sql_from_binlogevent, create_unique_file, reversed_lines


class Binlog2sql(object):
    def __init__(self, connectionSettings, startFile=None, startPos=None, endFile=None, endPos=None, startTime=None,
                 stopTime=None, only_schemas=None, only_tables=None, only_pk=False, nopk=False, flashback=False,
                 stopnever=False):
        '''
        connectionSettings: {'host': 127.0.0.1, 'port': 3306, 'user': slave, 'passwd': slave}
        '''
        if not startFile:
            raise ValueError('lack of parameter,startFile.')

        self.connectionSettings = connectionSettings
        self.startFile = startFile
        self.startPos = startPos if startPos else 4  # use binlog v4
        self.endFile = endFile if endFile else startFile
        self.endPos = endPos
        self.startTime = datetime.datetime.strptime(startTime,
                                                    "%Y-%m-%d %H:%M:%S") if startTime else datetime.datetime.strptime(
            '1970-01-01 00:00:00', "%Y-%m-%d %H:%M:%S")
        self.stopTime = datetime.datetime.strptime(stopTime,
                                                   "%Y-%m-%d %H:%M:%S") if stopTime else datetime.datetime.strptime(
            '2999-12-31 00:00:00', "%Y-%m-%d %H:%M:%S")

        self.only_schemas = only_schemas if only_schemas else None
        self.only_tables = only_tables if only_tables else None
        self.only_pk, self.nopk, self.flashback, self.stopnever = (only_pk, nopk, flashback, stopnever)

        self.binlogList = []
        self.connection = pymysql.connect(**self.connectionSettings)
        self.pk_list = {}
        try:
            cur = self.connection.cursor()
            cur.execute("SHOW MASTER STATUS")
            self.eofFile, self.eofPos = cur.fetchone()[:2]
            cur.execute("SHOW MASTER LOGS")
            binIndex = [row[0] for row in cur.fetchall()]
            if self.startFile not in binIndex:
                raise ValueError('parameter error: startFile %s not in mysql server' % self.startFile)
            binlog2i = lambda x: x.split('.')[1]
            for bin in binIndex:
                if binlog2i(bin) >= binlog2i(self.startFile) and binlog2i(bin) <= binlog2i(self.endFile):
                    self.binlogList.append(bin)

            cur.execute("SELECT @@server_id")
            self.serverId = cur.fetchone()[0]
            if not self.serverId:
                raise ValueError('need set server_id in mysql server %s:%s' % (
                    self.connectionSettings['host'], self.connectionSettings['port']))

            if self.only_pk and self.only_tables and self.only_schemas:
                cur.execute(
                    "SELECT table_schema,table_name, group_concat(column_name order by ordinal_position asc) pk_name \
                      FROM information_schema.KEY_COLUMN_USAGE \
                    WHERE table_schema IN (%s) \
                     AND table_name IN (%s) \
                     AND constraint_name='PRIMARY' \
                    GROUP BY table_schema,table_name" % (
                        "'" + "','".join(self.only_schemas) + "'", "'"+ "','".join(self.only_tables) + "'"))

                for row in cur.fetchall():
                    self.pk_list[row[0] + '.' + row[1]] = row[2].split(',')

        finally:
            cur.close()

    def process_binlog(self):
        stream = BinLogStreamReader(connection_settings=self.connectionSettings, server_id=self.serverId,
                                    log_file=self.startFile, log_pos=self.startPos, only_schemas=self.only_schemas,
                                    only_tables=self.only_tables, resume_stream=True)

        cur = self.connection.cursor()
        tmpFile = create_unique_file('%s.%s' % (self.connectionSettings['host'], self.connectionSettings[
            'port']))  # to simplify code, we do not use file lock for tmpFile.
        ftmp = open(tmpFile, "w")
        flagLastEvent = False
        eStartPos, lastPos = stream.log_pos, stream.log_pos
        try:
            for binlogevent in stream:
                # binlogevent.dump()
                if not self.stopnever:
                    if (stream.log_file == self.endFile and stream.log_pos == self.endPos) or (
                                    stream.log_file == self.eofFile and stream.log_pos == self.eofPos):
                        flagLastEvent = True
                    elif datetime.datetime.fromtimestamp(binlogevent.timestamp) < self.startTime:
                        if not (isinstance(binlogevent, RotateEvent) or isinstance(binlogevent,
                                                                                   FormatDescriptionEvent)):
                            lastPos = binlogevent.packet.log_pos
                        continue
                    elif (stream.log_file not in self.binlogList) \
                            or (self.endPos and stream.log_file == self.endFile and stream.log_pos > self.endPos) \
                            or (stream.log_file == self.eofFile and stream.log_pos > self.eofPos) \
                            or (datetime.datetime.fromtimestamp(binlogevent.timestamp) >= self.stopTime):
                        break
                        # else:
                        #     raise ValueError('unknown binlog file or position')

                if isinstance(binlogevent, QueryEvent) and binlogevent.query == 'BEGIN':
                    eStartPos = lastPos

                if isinstance(binlogevent, QueryEvent):
                    sql = concat_sql_from_binlogevent(cursor=cur, binlogevent=binlogevent, flashback=self.flashback,
                                                      nopk=self.nopk)
                    if sql:
                        print sql
                elif isinstance(binlogevent, WriteRowsEvent) or isinstance(binlogevent, UpdateRowsEvent) or isinstance(
                        binlogevent, DeleteRowsEvent):
                    for row in binlogevent.rows:
                        sql = concat_sql_from_binlogevent(cursor=cur, binlogevent=binlogevent, row=row,
                                                          flashback=self.flashback, nopk=self.nopk, eStartPos=eStartPos,
                                                          pk_list=self.pk_list)
                        if self.flashback:
                            ftmp.write(sql + '\n')
                        else:
                            print sql

                if not (isinstance(binlogevent, RotateEvent) or isinstance(binlogevent, FormatDescriptionEvent)):
                    lastPos = binlogevent.packet.log_pos
                if flagLastEvent:
                    break
            ftmp.close()

            if self.flashback:
                self.print_rollback_sql(tmpFile)
        except Exception, e:
            print traceback.format_exc()
        finally:
            os.remove(tmpFile)
        cur.close()
        stream.close()
        return True

    def print_rollback_sql(self, fin):
        '''print rollback sql from tmpfile'''
        with open(fin) as ftmp:
            sleepInterval = 1000
            i = 0
            for line in reversed_lines(ftmp):
                print line.rstrip()
                if i >= sleepInterval:
                    print 'SELECT SLEEP(1);'
                    i = 0
                else:
                    i += 1

    def __del__(self):
        pass


if __name__ == '__main__':
    # args = command_line_args(sys.argv[1:])
    args = command_line_args(
        '-utest -P3306 -ptest -h10.1.150.70 -d test sys -t t_city t --start-file=mysql-bin.000002 -B -PK'.split(' '))
    connectionSettings = {'host': args.host, 'port': args.port, 'user': args.user, 'passwd': args.password,'charset': 'utf8'}
    binlog2sql = Binlog2sql(connectionSettings=connectionSettings, startFile=args.startFile,
                            startPos=args.startPos, endFile=args.endFile, endPos=args.endPos,
                            startTime=args.startTime, stopTime=args.stopTime, only_schemas=args.databases,
                            only_tables=args.tables, only_pk=args.whereonlypk, nopk=args.nopk, flashback=args.flashback,
                            stopnever=args.stopnever)
    binlog2sql.process_binlog()