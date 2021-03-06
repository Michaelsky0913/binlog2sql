#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import argparse
import datetime
import getpass
import json
import logging
import colorlog
from contextlib import contextmanager
from pymysqlreplication.event import QueryEvent
from pymysqlreplication.row_event import (
    WriteRowsEvent,
    UpdateRowsEvent,
    DeleteRowsEvent,
)

# create a logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# set log format
log_colors_config = {
    'DEBUG': 'bold_puple',
    'INFO': 'bold_green',
    'WARNING': 'bold_yellow',
    'ERROR': 'bold_red',
    'CRITICAL': 'bold_red',
}
log_format = colorlog.ColoredFormatter(
    "%(log_color)s[%(asctime)s] [%(module)s:%(funcName)s] [%(lineno)d] [%(levelname)s] %(message)s", 
    log_colors=log_colors_config
)
# add a file handler
logfile = sys.path[0] + os.sep + sys.argv[0].split(os.sep)[-1].split(".")[0] + '.log'
logfile = "".join(sys.argv[0].split(".")[:-1]) + '.log'
file_handler = logging.FileHandler(logfile, mode='a')
file_handler.setFormatter(log_format)
logger.addHandler(file_handler)
# add a console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_format)
logger.addHandler(console_handler)

table = ''
err_flag = 0

if sys.version > '3':
    PY3PLUS = True
else:
    PY3PLUS = False


def is_valid_datetime(string):
    try:
        datetime.datetime.strptime(string, "%Y-%m-%d %H:%M:%S")
        return True
    except:
        return False


def create_unique_file(filename):
    version = 0
    result_file = filename
    # if we have to try more than 1000 times, something is seriously wrong
    while os.path.exists(result_file) and version < 1000:
        result_file = filename + '.' + str(version)
        version += 1
    if version >= 1000:
        raise OSError('cannot create unique file %s.[0-1000]' % filename)
    return result_file


@contextmanager
def temp_open(filename, mode):
    f = open(filename, mode)
    try:
        yield f
    finally:
        f.close()
        os.remove(filename)


def parse_args():
    """parse args for binlog2sql"""

    parser = argparse.ArgumentParser(description='Parse MySQL binlog to SQL you want', add_help=False)
    connect_setting = parser.add_argument_group('connect setting')
    connect_setting.add_argument('-h', '--host', dest='host', type=str,
                                 help='Host the MySQL database server located', default='127.0.0.1')
    connect_setting.add_argument('-u', '--user', dest='user', type=str,
                                 help='MySQL Username to log in as', default='root')
    connect_setting.add_argument('-p', '--password', dest='password', type=str, nargs='*',
                                 help='MySQL Password to use', default='')
    connect_setting.add_argument('-P', '--port', dest='port', type=int,
                                 help='MySQL port to use', default=3306)
    interval = parser.add_argument_group('interval filter')
    interval.add_argument('--start-file', dest='start_file', type=str, help='Start binlog file to be parsed')
    interval.add_argument('--start-position', '--start-pos', dest='start_pos', type=int,
                          help='Start position of the --start-file', default=4)
    interval.add_argument('--stop-file', '--end-file', dest='end_file', type=str,
                          help="Stop binlog file to be parsed. default: '--start-file'", default='')
    interval.add_argument('--stop-position', '--end-pos', dest='end_pos', type=int,
                          help="Stop position. default: latest position of '--stop-file'", default=0)
    interval.add_argument('--start-datetime', dest='start_time', type=str,
                          help="Start time. format %%Y-%%m-%%d %%H:%%M:%%S", default='')
    interval.add_argument('--stop-datetime', dest='stop_time', type=str,
                          help="Stop Time. format %%Y-%%m-%%d %%H:%%M:%%S;", default='')
    parser.add_argument('--stop-never', dest='stop_never', action='store_true', default=False,
                        help="Continuously parse binlog. default: stop at the latest event when you start.")
    parser.add_argument('--help', dest='help', action='store_true', help='help information', default=False)

    schema = parser.add_argument_group('schema filter')
    schema.add_argument('-d', '--databases', dest='databases', type=str, nargs='*',
                        help='dbs you want to process', default='')
    schema.add_argument('-t', '--tables', dest='tables', type=str, nargs='*',
                        help='tables you want to process', default='')

    event = parser.add_argument_group('type filter')
    event.add_argument('--only-dml', dest='only_dml', action='store_true', default=False,
                       help='only print dml, ignore ddl')
    event.add_argument('--sql-type', dest='sql_type', type=str, nargs='*', default=['INSERT', 'UPDATE', 'DELETE'],
                       help='Sql type you want to process, support INSERT, UPDATE, DELETE.')

    # exclusive = parser.add_mutually_exclusive_group()
    parser.add_argument('-K', '--no-primary-key', dest='no_pk', action='store_true',
                        help='Generate insert sql without primary key if exists', default=False)
    parser.add_argument('-B', '--flashback', dest='flashback', action='store_true',
                        help='Flashback data to start_position of start_file', default=False)
    parser.add_argument('--back-interval', dest='back_interval', type=float, default=1.0,
                        help="Sleep time between chunks of 1000 rollback sql. set it to 0 if do not need sleep")
    return parser


def command_line_args(args):
    need_print_help = False if args else True
    parser = parse_args()
    args = parser.parse_args(args)
    if args.help or need_print_help:
        parser.print_help()
        sys.exit(1)
    if not args.start_file:
        raise ValueError('Lack of parameter: start_file')
    if args.flashback and args.stop_never:
        raise ValueError('Only one of flashback or stop-never can be True')
    if args.flashback and args.no_pk:
        raise ValueError('Only one of flashback or no_pk can be True')
    if (args.start_time and not is_valid_datetime(args.start_time)) or \
            (args.stop_time and not is_valid_datetime(args.stop_time)):
        raise ValueError('Incorrect datetime argument')
    if not args.password:
        args.password = getpass.getpass()
    else:
        args.password = args.password[0]
    return args


def compare_items(items):
    # caution: if v is NULL, may need to process
    (k, v) = items
    if v is None:
        return '`%s` IS %%s' % k
    else:
        return '`%s`=%%s' % k


def fix_object_bytes(value: bytes):
    # logger.warning("fix_object_bytes 被调用")
    try:
        value = value.decode('utf-8')
    except Exception as e:
        # blob类型的数据解码不了
        logger.info("Failed to decode bytes object. We will skip it. This value comes from table: " + str(table))
        # 如果解码异常，则将异常标志置为1，后续不输出这个sql 
        global err_flag
        err_flag = 1
    # logger.info("所在表：" + table)
    # logger.info("bytes返回值：" + str(value))
    return value


def fix_object_array(value: list):
    # logger.warning("fix_object_array 被调用")
    new_list = []
    for v in value:
        # list里可能同时存在string、bytes(划重点)、array、json
        if isinstance(v, bytes):
            v = fix_object_bytes(v)
        elif isinstance(v, list):
            v = fix_object_array(v)
        elif isinstance(v, dict):
            v = fix_object_json(v)
        
        # string直接原封不动存储
        new_list.append(v)
    # logger.info("所在表：" + table)
    # logger.info("array返回值：" + str(new_list))
    return new_list


def fix_object_json(value: dict):
    # logger.warning("fix_object_json 被调用")
    new_dict = {}
    for k, v in value.items():
        # json内部 key 可能是字符串或bytes，如果是bytes，则跳转到bytes解析
        if isinstance(k, bytes):
            k = fix_object_bytes(k)
        
        # json内部的 value 则多种多样，可能为字符串、bytes(划重点)、array、json
        if isinstance(v, bytes):
            v = fix_object_bytes(v)
        elif isinstance(v, list):
            v = fix_object_array(v)
        elif isinstance(v, dict):
            v = fix_object_json(v)
        
        # 字符串直接赋值即可
        new_dict[k] = v
    # logger.info("所在表：" + table)
    # logger.info("json返回值：" + str(new_dict))
    return new_dict


def fix_object(value):
    """Fixes python objects so that they can be properly inserted into SQL queries"""
    if isinstance(value, set):
        value = ','.join(value)
    if PY3PLUS and isinstance(value, bytes):
        return fix_object_bytes(value)
    # 添加json数据解析
    elif PY3PLUS and isinstance(value, dict):
        return fix_object_json(value)
    # json里的数组解析
    elif PY3PLUS and isinstance(value, list):
        return fix_object_array(value)
    # python2 unicode
    elif not PY3PLUS and isinstance(value, unicode):
        return value.encode('utf-8')
    else:
        return value


def is_dml_event(event):
    if isinstance(event, WriteRowsEvent) or isinstance(event, UpdateRowsEvent) or isinstance(event, DeleteRowsEvent):
        return True
    else:
        return False


def event_type(event):
    t = None
    if isinstance(event, WriteRowsEvent):
        t = 'INSERT'
    elif isinstance(event, UpdateRowsEvent):
        t = 'UPDATE'
    elif isinstance(event, DeleteRowsEvent):
        t = 'DELETE'
    return t


def handle_list(value: list):
    # logger.info("handle_list 被调用")
    new_list = []
    for v in value:
        if isinstance(v, dict):
            try:
                v = json.dumps(v)
            except Exception as e:
                logger.error("Failed to dump dict value to string. Error is:" + str(e))
                logger.error("Error value is:" + str(v))
                sys.exit(1)
        elif isinstance(v, list):
            v = handle_list(v)
        new_list.append(v)
    return new_list


def concat_sql_from_binlog_event(cursor, binlog_event, row=None, e_start_pos=None, flashback=False, no_pk=False):
    if flashback and no_pk:
        raise ValueError('only one of flashback or no_pk can be True')
    if not (isinstance(binlog_event, WriteRowsEvent) or isinstance(binlog_event, UpdateRowsEvent)
            or isinstance(binlog_event, DeleteRowsEvent) or isinstance(binlog_event, QueryEvent)):
        raise ValueError('binlog_event must be WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent or QueryEvent')

    sql = ''
    if isinstance(binlog_event, WriteRowsEvent) or isinstance(binlog_event, UpdateRowsEvent) \
            or isinstance(binlog_event, DeleteRowsEvent):
        # 会调用 fix_object 函数生成sql
        pattern = generate_sql_pattern(binlog_event, row=row, flashback=flashback, no_pk=no_pk)
        
        # 过滤有异常标志的 sql，并将异常标志还原
        global err_flag
        if err_flag == 1:
            err_flag = 0
            return sql
        
        # cursor.mogrify 处理 value 时，会返回一个字符串，如果 value 里包含 dict，则会报错
        if isinstance(pattern['values'], list):
            pattern_values = handle_list(pattern['values'])
        else:
            pattern_values = pattern['values']
        sql = cursor.mogrify(pattern['template'], pattern_values)
        # 原始代码
        # sql = cursor.mogrify(pattern['template'], pattern['values'])
        time = datetime.datetime.fromtimestamp(binlog_event.timestamp)
        sql += ' #start %s end %s time %s' % (e_start_pos, binlog_event.packet.log_pos, time)
    elif flashback is False and isinstance(binlog_event, QueryEvent) and binlog_event.query != 'BEGIN' \
            and binlog_event.query != 'COMMIT':
        if binlog_event.schema:
            sql = 'USE {0};\n'.format(binlog_event.schema)
        sql += '{0};'.format(fix_object(binlog_event.query))

    return sql


def generate_sql_pattern(binlog_event, row=None, flashback=False, no_pk=False):
    template = ''
    values = []
    global table
    table = binlog_event.schema+'.'+binlog_event.table
    if flashback is True:
        if isinstance(binlog_event, WriteRowsEvent):
            template = 'DELETE FROM `{0}`.`{1}` WHERE {2} LIMIT 1;'.format(
                binlog_event.schema, binlog_event.table,
                ' AND '.join(map(compare_items, row['values'].items()))
            )
            values = map(fix_object, row['values'].values())
        elif isinstance(binlog_event, DeleteRowsEvent):
            template = 'INSERT INTO `{0}`.`{1}`({2}) VALUES ({3});'.format(
                binlog_event.schema, binlog_event.table,
                ', '.join(map(lambda key: '`%s`' % key, row['values'].keys())),
                ', '.join(['%s'] * len(row['values']))
            )
            values = map(fix_object, row['values'].values())
        elif isinstance(binlog_event, UpdateRowsEvent):
            template = 'UPDATE `{0}`.`{1}` SET {2} WHERE {3} LIMIT 1;'.format(
                binlog_event.schema, binlog_event.table,
                ', '.join(['`%s`=%%s' % x for x in row['before_values'].keys()]),
                ' AND '.join(map(compare_items, row['after_values'].items())))
            values = map(fix_object, list(row['before_values'].values())+list(row['after_values'].values()))
    else:
        if isinstance(binlog_event, WriteRowsEvent):
            if no_pk:
                # print binlog_event.__dict__
                # tableInfo = (binlog_event.table_map)[binlog_event.table_id]
                # if tableInfo.primary_key:
                #     row['values'].pop(tableInfo.primary_key)
                if binlog_event.primary_key:
                    row['values'].pop(binlog_event.primary_key)

            template = 'INSERT INTO `{0}`.`{1}`({2}) VALUES ({3});'.format(
                binlog_event.schema, binlog_event.table,
                ', '.join(map(lambda key: '`%s`' % key, row['values'].keys())),
                ', '.join(['%s'] * len(row['values']))
            )
            values = map(fix_object, row['values'].values())
        elif isinstance(binlog_event, DeleteRowsEvent):
            template = 'DELETE FROM `{0}`.`{1}` WHERE {2} LIMIT 1;'.format(
                binlog_event.schema, binlog_event.table, ' AND '.join(map(compare_items, row['values'].items())))
            values = map(fix_object, row['values'].values())
        elif isinstance(binlog_event, UpdateRowsEvent):
            template = 'UPDATE `{0}`.`{1}` SET {2} WHERE {3} LIMIT 1;'.format(
                binlog_event.schema, binlog_event.table,
                ', '.join(['`%s`=%%s' % k for k in row['after_values'].keys()]),
                ' AND '.join(map(compare_items, row['before_values'].items()))
            )
            values = map(fix_object, list(row['after_values'].values())+list(row['before_values'].values()))

    return {'template': template, 'values': list(values)}


def reversed_lines(fin):
    """Generate the lines of file in reverse order."""
    part = ''
    for block in reversed_blocks(fin):
        if PY3PLUS:
            # 解析回滚sql时，请确认对应的表没有blob数据类型，否则将会异常终止
            try:
                # block = block.decode("utf-8")
                block = fix_object(block)
            except Exception as e:
                logger.error("Error: " + str(e))
                logger.error("Could not decode block with utf8. skip the block " + str(block))
                logger.warning("Error block belongs to table: " + str(table))
                sys.exit(1)
                continue
        for c in reversed(block):
            if c == '\n' and part:
                yield part[::-1]
                part = ''
            part += c
    if part:
        yield part[::-1]


def reversed_blocks(fin, block_size=4096):
    """Generate blocks of file's contents in reverse order."""
    # 调到文件末尾
    fin.seek(0, os.SEEK_END)
    # 获取文件末尾的流位置
    here = fin.tell()
    # 如果文件非空
    while 0 < here:
        # 一次取固定大小的文件信息
        delta = min(block_size, here)
        here -= delta
        fin.seek(here, os.SEEK_SET)
        info = fin.read(delta)
        # print(info)
        yield info
        # yield fin.read(delta)
