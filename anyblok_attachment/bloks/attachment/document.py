# This file is a part of the AnyBlok / Attachment api project
#
#    Copyright (C) 2017 Jean-Sebastien SUZANNE <jssuzanne@anybox.fr>
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file,You can
# obtain one at http://mozilla.org/MPL/2.0/.
from anyblok.declarations import Declarations
from anyblok.column import (
    UUID, String, DateTime, Json, Integer, LargeBinary, Selection
)
from anyblok.relationship import One2One
from anyblok.field import Function
from datetime import datetime
from sqlalchemy import ForeignKeyConstraint, insert, update, select
from sqlalchemy.orm.session import object_state
from anyblok.common import anyblok_column_prefix
from uuid import uuid1
from .exceptions import (
    NoFileException, ProtectedFieldException
)


register = Declarations.register
Attachment = Declarations.Model.Attachment
Mixin = Declarations.Mixin


@register(Attachment)
class Document:
    DOCUMENT_TYPE = None

    uuid = UUID(primary_key=True, binary=False, nullable=False)
    version = String(primary_key=True, nullable=False)

    created_at = DateTime(default=datetime.now, nullable=False)
    # historied_at = DateTime()

    data = Json(default=dict)

    file_added_at = DateTime()
    filename = String(size=256)
    contenttype = String()
    filesize = Integer()
    file = LargeBinary()
    hash = String(size=256)

    type = Selection(selections={'latest': 'Latest', 'history': 'History'},
                     nullable=False)

    previous_version = One2One(model='Model.Attachment.Document',
                               backref="next_version")
    previous_versions = Function(fget="get_previous_versions")

    @classmethod
    def get_file_fields(cls):
        return ['file', 'file_added_at', 'contenttype', 'filename', 'hash',
                'filesize']

    def get_file(self):
        return self.to_dict(*self.get_file_fields())

    def get_previous_versions(self):
        res = []
        current = self
        while current.previous_version:
            current = current.previous_version
            res.append(current)

        return res

    @classmethod
    def define_mapper_args(cls):
        mapper_args = super(Document, cls).define_mapper_args()
        if cls.__registry_name__ == 'Model.Attachment.Document':
            mapper_args.update({'polymorphic_on': cls.type})

        mapper_args.update({'polymorphic_identity': cls.DOCUMENT_TYPE})
        return mapper_args

    @classmethod
    def insert(cls, *args, **kwargs):
        if cls.__registry_name__ == 'Model.Attachment.Document':
            return cls.registry.Attachment.Document.Latest.insert(
                *args, **kwargs)

        return super(Document, cls).insert(*args, **kwargs)


@register(Attachment.Document)
class Latest(Attachment.Document, Mixin.ForbidDelete):

    DOCUMENT_TYPE = 'latest'
    previous_versions = Function(fget="get_previous_versions")

    uuid = UUID(primary_key=True, binary=False, nullable=False)
    version = String(primary_key=True, nullable=False)

    @classmethod
    def define_table_args(cls):
        table_args = super(Latest, cls).define_table_args()
        D = cls.registry.Attachment.Document
        return table_args + (ForeignKeyConstraint(
            [cls.uuid, cls.version], [D.uuid, D.version],
            onupdate='cascade'),)

    @classmethod
    def insert(cls, *args, **kwargs):
        uuid = uuid1()
        code = 'Attachment.Document' + '#' + str(uuid)
        formater = "V-{seq:06d}"
        seq = cls.registry.System.Sequence.insert(code=code, formater=formater)
        values = kwargs.copy()
        values.update(dict(uuid=uuid, version=seq.nextval()))
        return super(Latest, cls).insert(*args, **values)

    def get_modified_fields(self):
        state = object_state(self)
        modified_fields = []
        for attr in state.manager.attributes:
            if not hasattr(attr.impl, 'get_history'):
                continue

            added, unmodified, deleted = attr.impl.get_history(
                state, state.dict)

            if added or deleted:
                field = attr.key
                if field.startswith(anyblok_column_prefix):
                    field = field[len(anyblok_column_prefix):]

                modified_fields.append(field)

        return modified_fields

    @classmethod
    def get_forbidden_modify_fields(cls):
        return ['uuid', 'version', 'creates_at',
                'attachment_document_uuid', 'attachment_document_version']

    @classmethod
    def before_update_orm_event(cls, mapper, connection, target):
        modified_fields = target.get_modified_fields()
        forbidden_fields = set(cls.get_forbidden_modify_fields())
        inter = set.intersection(forbidden_fields, set(modified_fields))
        if inter:
            raise ProtectedFieldException(
                "Protected fields %s, they can't be modified",
                list(inter)
            )

        Q = cls.query().filter(cls.uuid == target.uuid)
        Q = Q.filter(cls.version == target.version)
        Q = Q.filter(cls.file == None)  # noqa
        if Q.count():
            # No file in DB, then no archive is need
            return

        code = 'Attachment.Document' + '#' + str(target.uuid)
        Seq = cls.registry.System.Sequence
        seq = Seq.query().filter(Seq.code == code).one()
        old_version = target.version
        new_version = seq.nextval()

        Column = cls.registry.System.Column
        columns = Column.query().filter(
            Column.model == 'Model.Attachment.Document')
        columns = columns.all().name
        new_vals = {x: getattr(target, x) for x in columns}

        target.version = new_version
        target.created_at = datetime.now()
        target.previous_version = None

        if target.type != 'latest':
            target.type = 'latest'

        if 'file' not in modified_fields or target.file is None:
            for field in target.get_file_fields():
                setattr(target, field, None)

        document = cls.registry.Attachment.Document.__table__
        query = select([getattr(document.c, field)
                        for field in modified_fields])
        query = query.where(document.c.uuid == target.uuid)
        query = query.where(document.c.version == old_version)
        res = cls.registry.session.connection().execute(query)

        vals = res.fetchone()
        new_vals.update({x: vals[x] for x in modified_fields})
        new_vals['type'] = 'history'
        target.new_document = new_vals
        target.new_history = {
            'uuid': target.uuid,
            'version': old_version,
            'historied_at': datetime.now()
        }

    @classmethod
    def after_update_orm_event(cls, mapper, connection, target):
        print('after_update_orm_event')
        if (
            hasattr(target, 'new_history') and target.new_history and
            hasattr(target, 'new_document') and target.new_document
        ):
            # conn = cls.registry.session.connection()
            conn = cls.registry.session

            conn.execute(
                insert(
                    cls.registry.Attachment.Document.__table__
                ).values(**target.new_document)
            )
            conn.execute(
                insert(
                    cls.registry.Attachment.Document.History.__table__
                ).values(**target.new_history)
            )
            latest = cls.registry.Attachment.Document.__table__
            query = update(latest)
            query = query.where(latest.c.uuid == target.uuid)
            query = query.where(latest.c.version == target.version)
            query = query.values(
                attachment_document_uuid=target.new_history['uuid'],
                attachment_document_version=target.new_history['version'],
            )
            print(str(query))
            print(target.uuid, target.version)
            print(target.new_history)
            res = conn.execute(query)
            print(res.rowcount)
            delattr(target, 'new_history')
            delattr(target, 'new_document')

    def historize_a_copy(self):
        if not self.file:
            raise NoFileException(
                "Can not archive %s, because the document have not got file"
            )

        self.registry.flush()  # the file must be send to exist on db
        self.type = 'history'
        self.registry.flush()  # flush call the listen then do the archive

    def add_new_version(self):
        if not self.file:
            raise NoFileException(
                "Can not archive %s, because the document have not got file")

        for field in self.get_file_fields():
            setattr(self, field, None)

        self.registry.flush()  # flush call the listen then do the archive


@register(Attachment.Document)
class History(Attachment.Document, Mixin.ReadOnly):

    DOCUMENT_TYPE = 'history'

    uuid = UUID(primary_key=True, binary=False, nullable=False)
    version = String(primary_key=True, nullable=False)
    historied_at = DateTime()

    @classmethod
    def define_table_args(cls):
        table_args = super(History, cls).define_table_args()
        D = cls.registry.Attachment.Document
        return table_args + (ForeignKeyConstraint(
            [cls.uuid, cls.version], [D.uuid, D.version]),)