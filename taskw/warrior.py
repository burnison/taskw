""" Code to interact with taskwarrior

This module contains an abstract base class and two different implementations
for interacting with taskwarrior:  TaskWarrior and TaskWarriorExperimental.

"""

import abc
import codecs
import os
import re
import time
import uuid
import subprocess
import json
import pprint

import taskw.utils

import six
from six import with_metaclass
from six.moves import filter
from six.moves import map
from six.moves import zip


open = lambda fname, mode: codecs.open(fname, mode, "utf-8")


class TaskWarriorBase(with_metaclass(abc.ABCMeta, object)):
    """ The task warrior

    Really though, a python object with methods allowing you to interact
    with a taskwarrior database.
    """

    def __init__(self, config_filename="~/.taskrc"):
        self.config_filename = config_filename
        self.config = TaskWarriorBase.load_config(config_filename)

    def _stub_task(self, description, tags=None, **kw):
        """ Given a description, stub out a task dict. """

        task = {"description": description}

        if tags is not None:
            task['tags'] = tags

        task.update(kw)

        # Only UNIX timestamps are currently supported.
        if 'due' in kw:
            task['due'] = str(task['due'])

        return task

    def _extract_annotations_from_task(self, task):
        """ Removes annotations from a task and returns a list of annotations
        """
        annotations = list()
        for key in task.keys():
            if key.startswith('annotation_'):
                annotations.append(task[key])
                del(task[key])
        return annotations

    @abc.abstractmethod
    def load_tasks(self):
        """ Load all tasks.

        >>> w = Warrior()
        >>> tasks = w.load_tasks()
        >>> tasks.keys()
        ['completed', 'pending']
        >>> type(tasks['pending'])
        <type 'list'>
        >>> type(tasks['pending'][0])
        <type 'dict'>

        """

    @abc.abstractmethod
    def task_add(self, description, tags=None, **kw):
        """ Add a new task.

        Takes any of the keywords allowed by taskwarrior like proj or prior.
        """
        pass

    @abc.abstractmethod
    def task_done(self, **kw):
        pass

    @abc.abstractmethod
    def task_update(self, task):
        pass

    def get_task(self, **kw):
        valid_keys = set(['id', 'uuid', 'description'])
        id_keys = valid_keys.intersection(kw.keys())

        if len(id_keys) != 1:
            raise KeyError("get_task must receive one ID keyword argument")

        key = list(id_keys)[0]
        if key not in valid_keys:
            raise KeyError("Argument must be one of %r" % valid_keys)

        statuses = list()
        if 'status' in kw:
            statuses.append(kw[six.u('status')])
            # @TODO: Valid options are 'pending' or 'completed'
        else:
            statuses = ['pending', 'completed']

        tasks = self.load_tasks()

        id = None
        task = dict()

        for status in statuses:
            if key == 'id':
                id = kw[key]
                if len(tasks[status]) < int(id):
                    continue

                task = tasks[status][int(id) - 1]
                return id, task
            else:
                matching = list(filter(
                    lambda t: t[key] == kw[key],
                    tasks[status]
                ))

                if not matching:
                    continue

                task = matching[0]
                id = tasks[status].index(task) + 1
                return id, task

        return id, task

    def filter_by(self, func):
        tasks = self.load_tasks()
        filtered = filter(func, tasks)
        return filtered

    @classmethod
    def load_config(self, config_filename="~/.taskrc"):
        """ Load ~/.taskrc into a python dict

        >>> config = TaskWarrior.load_config()
        >>> config['data']['location']
        '/home/threebean/.task'
        >>> config['_forcecolor']
        'yes'

        """

        with open(os.path.expanduser(config_filename), 'r') as f:
            lines = f.readlines()

        _usable = lambda l: not(l.startswith('#') or l.strip() == '')
        lines = filter(_usable, lines)

        def _build_config(key, value, d):
            """ Called recursively to split up keys """
            pieces = key.split('.', 1)
            if len(pieces) == 1:
                d[pieces[0]] = value.strip()
            else:
                d[pieces[0]] = _build_config(pieces[1], value, {})

            return d

        d = {}
        for line in lines:
            if '=' not in line:
                continue

            key, value = line.split('=')
            d = _build_config(key, value, d)

        return d


class TaskWarrior(TaskWarriorBase):
    """ Interacts with taskwarrior by directly manipulating the ~/.task/ db.

    Currently this is the supported implementation, but will be phased out in
    time due to taskwarrior's guidelines:  http://bit.ly/16I9VN4

    See https://github.com/ralphbean/taskw/pull/15 for discussion.
    """

    def load_tasks(self):
        def _load_tasks(filename):
            filename = os.path.join(self.config['data']['location'], filename)
            filename = os.path.expanduser(filename)
            with open(filename, 'r') as f:
                lines = f.readlines()

            return list(map(taskw.utils.decode_task, lines))

        return dict(
            (db, _load_tasks('%s.data' % db))
            for db in ['completed', 'pending']
        )

    def task_add(self, description, tags=None, **kw):
        """ Add a new task.

        Takes any of the keywords allowed by taskwarrior like proj or prior.
        """

        task = self._stub_task(description, tags, **kw)

        task['status'] = 'pending'

        # TODO -- check only valid keywords

        if not 'entry' in task:
            task['entry'] = str(int(time.time()))

        if not 'uuid' in task:
            task['uuid'] = str(uuid.uuid4())

        id = self._task_add(task, 'pending')
        task['id'] = id
        return task

    def task_done(self, **kw):
        id, task = self.get_task(**kw)

        task['status'] = 'completed'
        task['end'] = kw.get('end') or str(int(time.time()))

        self._task_add(task, 'completed')
        self._task_remove(id, 'pending')
        return task

    def task_update(self, task):
        id, _task = self.get_task(uuid=task['uuid'])

        if 'id' in task:
            del task['id']

        _task.update(task)
        self._task_replace(id, 'pending', _task)
        return id, _task

    def _task_replace(self, id, category, task):
        def modification(lines):
            lines[id - 1] = taskw.utils.encode_task(task)
            return lines

        # FIXME write to undo.data
        self._apply_modification(id, category, modification)

    def _task_remove(self, id, category):
        def modification(lines):
            del lines[id - 1]
            return lines

        # FIXME write to undo.data
        self._apply_modification(id, category, modification)

    def _apply_modification(self, id, category, modification):
        location = self.config['data']['location']
        filename = category + '.data'
        filename = os.path.join(self.config['data']['location'], filename)
        filename = os.path.expanduser(filename)

        with open(filename, "r") as f:
            lines = f.readlines()

        lines = modification(lines)

        with open(filename, "w") as f:
            f.writelines(lines)

    def _task_add(self, task, category):
        location = self.config['data']['location']
        location = os.path.expanduser(location)
        filename = category + '.data'

        # Append the task
        with open(os.path.join(location, filename), "a") as f:
            f.writelines([taskw.utils.encode_task(task)])

        # FIXME - this gets written when a task is completed.  incorrect.
        # Add to undo.data
        with open(os.path.join(location, 'undo.data'), "a") as f:
            f.write("time %s\n" % str(int(time.time())))
            f.write("new %s" % taskw.utils.encode_task(task))
            f.write("---\n")

        with open(os.path.join(location, filename), "r") as f:
            # The 'id' of this latest added task.
            return len(f.readlines())


class TaskWarriorExperimental(TaskWarriorBase):
    """ Interacts with taskwarrior by invoking shell commands.

    This is currently experimental and is not necessarily stable.  Please help
    us test and report any issues.

    Some day this will become the primary supported implementation due to
    taskwarrior's guidelines:  http://bit.ly/16I9VN4

    See https://github.com/ralphbean/taskw/pull/15 for discussion.
    """

    @classmethod
    def can_use(cls):
        """ Returns true if runtime requirements of experimental mode are met
        """
        if not os.path.exists('/usr/bin/task'):
            return False

        taskwarrior_version = subprocess.Popen(
            ['task', '--version'],
            stdout=subprocess.PIPE
        ).communicate()[0]
        taskwarrior_version = map(int, taskwarrior_version.split('.'))
        return taskwarrior_version >= [2, 0, 0]

    def load_tasks(self):
        # Load tasks using `task export`
        pending_tasks = list()
        completed_tasks = list()
        tasks = dict()
        pending_tasks = json.loads(subprocess.Popen([
            'task', 'rc:%s' % self.config_filename,
            'rc.json.array=TRUE', 'rc.verbose=nothing', 'status:pending',
            'export'], stdout=subprocess.PIPE).communicate()[0])
        completed_tasks = json.loads(subprocess.Popen([
            'task', 'rc:%s' % self.config_filename,
            'rc.json.array=TRUE', 'rc.verbose=nothing', 'status:completed',
            'export'], stdout=subprocess.PIPE).communicate()[0])
        tasks['pending'] = pending_tasks
        tasks['completed'] = completed_tasks
        return tasks

    def task_add(self, description, tags=None, **kw):
        """ Add a new task.

        Takes any of the keywords allowed by taskwarrior like proj or prior.
        """

        task = self._stub_task(description, tags, **kw)

        # Check if there are annotations, if so remove them from the
        # task and add them after we've added the task.
        annotations = self._extract_annotations_from_task(task)
        subprocess.call([
            'task', 'rc:%s' % self.config_filename,
            'rc.verbose=nothing',
            'add', taskw.utils.encode_task_experimental(task)])
        id, added_task = self.get_task(description=task['description'])
        if annotations:
            for annotation in annotations:
                self.task_annotate(added_task, annotation)
        id, added_task = self.get_task(uuid=added_task[six.u('uuid')])
        return added_task

    def task_annotate(self, task, annotation):
        """ Annotates a task. """
        subprocess.call([
            'task', 'rc:%s' % self.config_filename,
            'rc.verbose=nothing', str(task[six.u('uuid')]),
            'annotate', annotation])
        id, annotated_task = self.get_task(uuid=task[six.u('uuid')])
        return annotated_task

    def task_done(self, **kw):
        id, task = self.get_task(**kw)

        subprocess.Popen([
            'task', 'rc:%s' % self.config_filename,
            'rc.verbose=nothing', str(id),
            'do'], stdout=subprocess.PIPE).communicate()[0]
        tasks = self.load_tasks()
        return tasks['completed'][-1]

    def task_update(self, task):
        id, _task = self.get_task(uuid=task['uuid'])

        if 'id' in task:
            del task['id']

        _task.update(task)

        # Unset task attributes that should not be updated
        task_to_modify = _task
        del task_to_modify['uuid']
        del task_to_modify['id']

        # Check if there are annotations, if so, look if they are
        # in the existing task, otherwise annotate the task to add them.
        new_annotations = self._extract_annotations_from_task(task)
        existing_annotations = \
            self._extract_annotations_from_task(task_to_modify)

        if 'annotations' in task_to_modify:
            del task_to_modify['annotations']

        modification = taskw.utils.encode_task_experimental(task_to_modify)
        subprocess.call([
            'task', 'rc:%s' % self.config_filename,
            'rc.verbose=nothing', task[six.u('uuid')], 'modify', modification])

        # If there are no existing annotations, add the new ones
        if existing_annotations is None:
            for annotation in new_annotations:
                self.task_annotate(task_to_modify, annotation)

        # If there are existing annotations and new annotations, add only
        # the new annotations
        if existing_annotations is not None and new_annotations is not None:
            for annotation in new_annotations:
                if annotation not in existing_annotations:
                    self.task_annotate(task_to_modify, annotation)

        return id, _task

    def task_info(self, **kw):
        id, task = self.get_task(**kw)
        info = subprocess.Popen([
            'task', 'rc:%s' % self.config_filename,
            'rc.verbose=nothing', str(id),
            'info'],
            stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        out, err = info.communicate()
        if err:
            return err
        return out
