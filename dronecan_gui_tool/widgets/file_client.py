#
# Copyright (C) 2016  UAVCAN Development Team  <uavcan.org>
#
# This software is distributed under the terms of the MIT License.
#
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import dronecan
from dronecan import uavcan
import os
import json
import zlib
import base64
import struct
from PyQt5.QtWidgets import QGroupBox, QVBoxLayout, QHBoxLayout, QWidget, QDirModel, QCompleter, QFileDialog, QLabel
from PyQt5.QtCore import QTimer
from logging import getLogger
from . import make_icon_button, CommitableComboBoxWithHistory, get_icon, flash, LabelWithIcon


logger = getLogger(__name__)

def FileClient_PathKey(path):
    '''
    return key used in file read request for a path. This is kept to 7 bytes
    to keep the read request in 2 frames
    '''
    return base64.b64encode(struct.pack("<I",zlib.crc32(bytearray(path,'utf-8'))))[:7].decode('utf-8')

class PathItem(QWidget):
    def __init__(self, parent, default=None):
        super(PathItem, self).__init__(parent)

        self.on_remove = lambda _: None
        self.on_path_changed = lambda *_: None

        self._remove_button = make_icon_button('remove', 'Remove this path', self,
                                               on_clicked=lambda: self.on_remove(self))

        completer = QCompleter(self)
        completer.setModel(QDirModel(completer))

        self._path_bar = CommitableComboBoxWithHistory(self)
        if default:
            self._path_bar.setCurrentText(default)
        self._path_bar.setCompleter(completer)
        self._path_bar.setAcceptDrops(True)
        self._path_bar.setToolTip('Lookup path for file services; should point either to a file or to a directory')
        self._path_bar.currentTextChanged.connect(self._on_path_changed)

        self._select_file_button = make_icon_button('file-o', 'Specify file path', self,
                                                    on_clicked=self._on_select_path_file)

        self._select_dir_button = make_icon_button('folder-open-o', 'Specify directory path', self,
                                                   on_clicked=self._on_select_path_directory)

        # self._hit_read_label = make_icon_button('download', 'Read', self,
        #                                       checkable=True,
        #                                       on_clicked=self._file_client._read_call)

        self._hit_write_label = LabelWithIcon(get_icon('upload'), 'Write', self)
        self._hit_write_label.setToolTip('Write')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._remove_button)
        layout.addWidget(self._path_bar, 1)
        layout.addWidget(self._select_file_button)
        layout.addWidget(self._select_dir_button)
        layout.addWidget(self._hit_write_label)
        self.setLayout(layout)

    def _on_path_changed(self):
        # self.reset_hit_counts()
        self.on_path_changed()

    def _on_select_path_file(self):
        path = QFileDialog().getOpenFileName(self, 'Add file path to be served by the file client',
                                             os.path.expanduser('~'))
        self._path_bar.setCurrentText(path[0])

    def _on_select_path_directory(self):
        path = QFileDialog().getExistingDirectory(self, 'Add directory lookup path for the file client',
                                                  os.path.expanduser('~'))
        self._path_bar.setCurrentText(path)

    @property
    def path(self):
        p = self._path_bar.currentText()
        return p

    def update_hit_count(self, _path, hit_count):
        self._hit_count_label.setText(str(hit_count))

    def reset_hit_counts(self):
        self._hit_count_label.setText('0')

class FileClientJson(dronecan.app.file_client.FileClient):
    def __init__(self, node):
        super(FileClientJson, self).__init__(node)
        self._images = {}
        self._image_timestamps = {}
        self._key_to_path = {}

        self._total_transaction = 0
        self._is_incomplete = False
        self.node = node

    def request(self, req, node_id, callback):
        self.node.request(req, node_id, callback)

    def _resolve_path(self, relative):
        rel = relative.path.decode().replace(chr(relativePARATOR), os.path.sep)
        if rel in self._key_to_path:
            return self._key_to_path[rel]
        return super(FileClientJson, self)._resolve_path(relative)

    def _load_image(self, path):
        if path.lower().endswith('.apj') or path.lower().endswith('.px4'):
            # load JSON image
            j = json.load(open(path,'r'))
            if not 'image' in j:
                print("Missing image in %s" % path)
                return None
            return bytearray(zlib.decompress(base64.b64decode(j['image'])))
        return open(path,'rb').read()

    def _check_path_change(self, path):
        mtime = os.path.getmtime(path)
        if path not in self._images or mtime != self._image_timestamps[path]:
            self._image_timestamps[path] = mtime
            self._images[path] = self._load_image(path)
            self._key_to_path[FileClient_PathKey(path)] = path
    
    def _read(self, e):
        self._is_incomplete = len(e.response.data.data) < 256
        if self._is_incomplete:
            self._total_transaction += len(e.response.data.data)

    def _read_call(self, path="@SYS/t.txt", node_id=127):
        # logger.debug("[#{0:03d}:uavcan.protocol.file.Read] {1!r} @ offset {2:d}"
        #              .format(e.transfer.source_node_id, e.request.path.path.decode(), e.request.offset))
        path="@SYS/t.txt"
        try:
            req = uavcan.protocol.file.Read.Request()
            if not self._is_incomplete:
                req.offset = self._total_transaction
                print(path)
                req.path.path = path.encode()
                self.request(req, node_id, self._read)

                return True
        except Exception:
            logger.exception("[#{0:03d}:uavcan.protocol.file.Read] error")
            # resp = uavcan.protocol.file.Read.Response()
            # resp.error.value = resp.error.UNKNOWN_ERROR

        return False


class FileClientWidget(QGroupBox):
    def __init__(self, parent, node):
        super(FileClientWidget, self).__init__(parent)
        self.setTitle('File client (dronecan.uavcan.protocol.file.*)')

        self._node = node
        self._file_client = None

        self._path_widgets = []

        self._start_button = make_icon_button('rocket', 'Launch/stop the file client', self,
                                              checkable=True,
                                              on_clicked=self._on_start_stop)
        self._start_button.setEnabled(False)

        self._tmr = QTimer(self)
        self._tmr.setSingleShot(False)
        self._tmr.timeout.connect(self._update_on_timer)
        self._tmr.start(500)

        self._add_path_button = \
            make_icon_button('plus', 'Add lookup path (lookup paths can be modified while the client is running)',
                             self, on_clicked=self._on_add_path)
        
        self._hit_read_label = make_icon_button('download', 'Read', self,
                                              checkable=True)

        layout = QVBoxLayout(self)

        controls_layout = QHBoxLayout(self)
        controls_layout.addWidget(self._start_button)
        controls_layout.addWidget(self._add_path_button)
        controls_layout.addWidget(self._hit_read_label)
        controls_layout.addStretch(1)

        layout.addLayout(controls_layout)
        self.setLayout(layout)

    def _update_on_timer(self):
        self._start_button.setEnabled(not self._node.is_anonymous)
        self._start_button.setChecked(self._file_client is not None)
        if self._file_client:
            for path, count in self._file_client.path_hit_counters.items():
                for w in self._path_widgets:
                    if path.startswith(w.path):
                        w.update_hit_count(path, count)
        else:
            for w in self._path_widgets:
                w.reset_hit_counts()

    def _get_paths(self):
        return [x.path for x in self._path_widgets if x.path]

    def _sync_paths(self):
        if self._file_client:
            paths = self._get_paths()
            logger.info('Updating lookup paths: %r', paths)
            self._file_client.lookup_paths = paths
            flash(self, 'File client lookup paths: %r', paths, duration=3)

    def _on_start_stop(self):
        if self._file_client:
            try:
                self._file_client.close()
            except Exception:
                logger.error('Could not stop file client', exc_info=True)
            self._file_client = None
            logger.info('File client stopped')
        else:
            self._file_client = FileClientJson(self._node)
            self._hit_read_label.clicked.connect(self._file_client._read_call)

            self._sync_paths()

    def _on_remove_path(self, path):
        orig_len = len(self._path_widgets)
        self._path_widgets.remove(path)
        assert orig_len - 1 == len(self._path_widgets)

        self.layout().removeWidget(path)
        path.setParent(None)
        path.deleteLater()

        self._sync_paths()

    def _on_add_path(self, default=None):
        new = PathItem(self, default)
        new.on_path_changed = self._sync_paths
        new.on_remove = self._on_remove_path

        self._path_widgets.append(new)
        self.layout().addWidget(new)

        self._sync_paths()

    def add_path(self, path):
        path = os.path.normcase(os.path.abspath(os.path.expanduser(path)))

        for it in self._path_widgets:
            if it.path == path:
                return                  # Already exists, no need to add

        self._on_add_path(path)

    def force_start(self):
        if not self._file_client:
            self._on_start_stop()

