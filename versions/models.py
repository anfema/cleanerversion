# Copyright 2014 Swisscom, Sophia Engineering
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import datetime
import uuid
from django import VERSION

if VERSION[:2] >= (1, 7):
    from django.apps.registry import apps
from django.core.exceptions import SuspiciousOperation, MultipleObjectsReturned, ObjectDoesNotExist
from django.db.models.base import Model
from django.db.models import Q
from django.db.models.fields import FieldDoesNotExist
from django.db.models.fields.related import (ForeignKey, ReverseSingleRelatedObjectDescriptor,
    ReverseManyRelatedObjectsDescriptor, ManyToManyField, ManyRelatedObjectsDescriptor, create_many_related_manager,
    ForeignRelatedObjectsDescriptor, ManyToOneRel)
from django.db.models.query import QuerySet, ValuesListQuerySet, ValuesQuerySet
from django.db.models.signals import post_init
from django.db.models.sql import Query
from django.db.models.sql.where import ExtraWhere
from django.utils.functional import cached_property
from django.utils.timezone import utc
from django.utils import six

from django.db import models, router


def get_utc_now():
    return datetime.datetime.utcnow().replace(tzinfo=utc)


class VersionManager(models.Manager):
    """
    This is the Manager-class for any class that inherits from Versionable
    """
    use_for_related_fields = True

    def get_queryset(self):
        return VersionedQuerySet(self.model, using=self._db)

    def as_of(self, time=None):
        """
        Filters Versionables at a given time
        :param time: The timestamp (including timezone info) at which Versionables shall be retrieved
        :return: A QuerySet containing the base for a timestamped query.
        """
        return self.get_queryset().as_of(time)

    def next_version(self, object):
        """
        Return the next version of the given object. In case there is no next object existing, meaning the given
        object is the current version, the function returns this version.
        """
        if object.version_end_date == None:
            return object
        else:
            try:
                next = self.get(
                    Q(identity=object.identity),
                    Q(version_start_date=object.version_end_date))
            except MultipleObjectsReturned as e:
                raise MultipleObjectsReturned(
                    "next_version couldn't uniquely identify the next version of object " + str(
                        object.identity) + " to be returned\n" + str(e))
            except ObjectDoesNotExist as e:
                raise ObjectDoesNotExist(
                    "next_version couldn't find a next version of object " + str(object.identity) + "\n" + str(e))
            return next

    def previous_version(self, object):
        """
        Return the previous version of the given object. In case there is no previous object existing, meaning the
        given object is the first version of the object, then the function returns this version.
        """
        if object.version_birth_date == object.version_start_date:
            return object
        else:
            try:
                previous = self.get(
                    Q(identity=object.identity),
                    Q(version_end_date=object.version_start_date))
            except MultipleObjectsReturned as e:
                raise MultipleObjectsReturned(
                    "pervious_version couldn't uniquely identify the previous version of object " + str(
                        object.identity) + " to be returned\n" + str(e))
            # This should never-ever happen, since going prior a first version of an object should be avoided by the
            # first test of this method
            except ObjectDoesNotExist as e:
                raise ObjectDoesNotExist(
                    "pervious_version couldn't find a previous version of object " + str(object.identity) + "\n" + str(
                        e))
            return previous

    def current_version(self, object):
        """
        Return the current version of the given object. The current version is the one having its version_end_date set
        to NULL. If there is not such a version then it means the object has been 'deleted' and so there is no
        current version available. In this case the function returns None.
        """
        if object.version_end_date == None:
            return object

        return self.current.filter(identity=object.identity).first()

    @property
    def current(self):
        return self.as_of(None)

    def create(self, **kwargs):
        """
        Creates an instance of a Versionable
        :param kwargs: arguments used to initialize the class instance
        :return: a Versionable instance of the class
        """
        return self._create_at(None, **kwargs)

    def _create_at(self, timestamp=None, **kwargs):
        """
        WARNING: Only for internal use and testing.

        Create a Versionable having a version_start_date and version_birth_date set to some pre-defined timestamp
        :param timestamp: point in time at which the instance has to be created
        :param kwargs: arguments needed for initializing the instance
        :return: an instance of the class
        """
        ident = six.u(str(uuid.uuid4()))
        if timestamp is None:
            timestamp = get_utc_now()
        kwargs['id'] = ident
        kwargs['identity'] = ident
        kwargs['version_start_date'] = timestamp
        kwargs['version_birth_date'] = timestamp
        return super(VersionManager, self).create(**kwargs)

class VersionedQuery(Query):
    """
    VersionedQuery has awareness of the query time restrictions.  When the query is compiled,
    this query time information is passed along to the foreign keys involved in the query, so
    that they can provide that information when building the sql.
    """

    def __init__(self, *args, **kwargs):
        super(VersionedQuery, self).__init__(*args, **kwargs)
        self.set_as_of(None, False)

    def clone(self, *args, **kwargs):
        obj = super(VersionedQuery, self).clone(*args, **kwargs)
        try:
            obj.set_as_of(self.as_of_time, self.apply_as_of_time)
        except AttributeError:
            # If the caller is using clone to create a different type of Query, that's OK.
            # An example of this is when creating or updating an object, this method is called
            # with a first parameter of sql.UpdateQuery.
            pass
        return obj

    def set_as_of(self, as_of_time, apply_as_of_time):
        """
        Set the as_of time that will be used to restrict the query for the valid objects.
        :param DateTime as_of_time: Datetime or None (None means use the current objects)
        :param bool apply_as_of_time: If false, then the query will not be restricted by as_of_time
        :return:
        """
        self.as_of_time = as_of_time
        self.apply_as_of_time = apply_as_of_time

    def get_compiler(self, *args, **kwargs):
        # Wait! One more thing before returning the compiler:
        # propagate the query time to all the related foreign key fields.
        self.propagate_query_time()
        return super(VersionedQuery, self).get_compiler(*args, **kwargs)

    def propagate_query_time(self):
        """
        This sets as query time, or lack of query time, on all the foreign keys
        involved in the query.  Only if they are aware of the query time can they
        create a time-based restriction in the JOIN ON clause.  In the case of
        left outer joins, it is necessary that the time-based restriction happens
        in the JOIN ON clause.  For inner joins, it doesn't hurt.
        """
        first = True
        for alias in self.tables:
            if not self.alias_refcount[alias]:
                continue
            try:
                name, alias, join_type, lhs, join_cols, _, join_field = self.alias_map[alias]
            except KeyError:
                # Extra tables can end up in self.tables, but not in the
                # alias_map if they aren't in a join. That's OK. We skip them.
                continue
            if join_type and not first:
                join_field.set_as_of(self.as_of_time, self.apply_as_of_time)
            first = False

class VersionedQuerySet(QuerySet):
    """
    The VersionedQuerySet makes sure that every objects retrieved from it has
    the added property 'query_time' added to it.
    For that matter it override the __getitem__, _fetch_all and _clone methods
    for its parent class (QuerySet).
    """

    def __init__(self, model=None, query=None, *args, **kwargs):
        """
        Overridden so that a VersionedQuery will be used.
        """
        if not query:
            query = VersionedQuery(model)
        super(VersionedQuerySet, self).__init__(model=model, query=query, *args, **kwargs)
        self.query_time = None

    def __getitem__(self, k):
        """
        Overrides the QuerySet.__getitem__ magic method for retrieving a list-item out of a query set.
        :param k: Retrieve the k-th element or a range of elements
        :return: Either one element or a list of elements
        """
        item = super(VersionedQuerySet, self).__getitem__(k)
        if isinstance(item, (list,)):
            for i in item:
                self._set_query_time(i)
        else:
            self._set_query_time(item)
        return item

    def _fetch_all(self):
        """
        Completely overrides the QuerySet._fetch_all method by adding the timestamp to all objects
        :return: See django.db.models.query.QuerySet._fetch_all for return values
        """
        if self._result_cache is None:
            self._result_cache = list(self.iterator())
            if not isinstance(self, ValuesListQuerySet):
                for x in self._result_cache:
                    self._set_query_time(x)
        if self._prefetch_related_lookups and not self._prefetch_done:
            self._prefetch_related_objects()

    def _clone(self, *args, **kwargs):
        """
        Overrides the QuerySet._clone method by adding the cloning of the VersionedQuerySet's query_time parameter
        :param kwargs: Same as the original QuerySet._clone params
        :return: Just as QuerySet._clone, this method returns a clone of the original object
        """
        if VERSION[:2] == (1, 6):
            klass = kwargs.pop('klass', None)
            # This patch was taken from Django 1.7 and is applied only in case we're using Django 1.6 and
            # ValuesListQuerySet objects. Since VersionedQuerySet is not a subclass of ValuesListQuerySet, a new type
            # inheriting from both is created and used as class.
            # https://github.com/django/django/blob/1.7/django/db/models/query.py#L943
            if klass and not issubclass(self.__class__, klass):
                base_queryset_class = getattr(self, '_base_queryset_class', self.__class__)
                class_bases = (klass, base_queryset_class)
                class_dict = {
                    '_base_queryset_class': base_queryset_class,
                    '_specialized_queryset_class': klass,
                }
                kwargs['klass'] = type(klass.__name__, class_bases, class_dict)
            else:
                kwargs['klass'] = klass

        clone = super(VersionedQuerySet, self)._clone(**kwargs)
        clone.query_time = self.query_time

        return clone

    def _set_query_time(self, item, type_check=True):
        """
        Sets the time for which the query was made on the resulting item
        :param item: an item of type Versionable
        :param type_check: Check the item to be a Versionable
        :return: Returns the item itself with the time set
        """
        if isinstance(item, Versionable):
            item.as_of = self.query_time
        elif isinstance(item, VersionedQuerySet):
            item.query_time = self.query_time
        elif isinstance(self, ValuesQuerySet):
            # When we are dealing with a ValueQuerySet there is no point in
            # setting the query_time as we are returning an array of values
            # instead of a full-fledged model object
            pass
        else:
            if type_check:
                raise TypeError("This item is not a Versionable, it's a " + str(type(item)))
        return item

    def as_of(self, qtime=None):
        """
        Sets the time for which we want to retrieve an object.
        :param qtime: The UTC date and time; if None then use the current state (where version_end_date = NULL)
        :return: A VersionedQuerySet
        """
        self.query.set_as_of(qtime, True)
        return self.add_as_of_filter(qtime)

    def add_as_of_filter(self, querytime):
        """
        Add a version time restriction filter to the given queryset.

        If querytime = None, then the filter will simply restrict to the current objects (those
        with version_end_date = NULL).

        :param querytime: UTC datetime object, or None.
        :return: VersionedQuerySet
        """
        if querytime:
            self.query_time = querytime
            filter = (Q(version_end_date__gt=querytime) | Q(version_end_date__isnull=True)) \
                     & Q(version_start_date__lte=querytime)
        else:
            filter = Q(version_end_date__isnull=True)
        return self.filter(filter)

    def values_list(self, *fields, **kwargs):
        """
        Overridden so that an as_of filter will be added to the queryset returned by the parent method.
        """
        qs = super(VersionedQuerySet, self).values_list(*fields, **kwargs)
        return qs.add_as_of_filter(qs.query_time)


class VersionedManyToOneRel(ManyToOneRel):
    """
    Overridden to allow keeping track of the query as_of time, so that foreign keys
    can use that information when creating the sql joins.
    """

    def set_as_of(self, as_of_time, apply_as_of_time):
        """
        Set the as_of time that will be used to restrict the query for the valid objects.
        :param DateTime as_of_time: Datetime or None (None means use the current objects)
        :param bool apply_as_of_time: If false, then the query will not be restricted by as_of_time
        :return:
        """
        self.as_of_time = as_of_time
        self.apply_as_of_time = apply_as_of_time
        if hasattr(self, 'field'):
            self.field.set_as_of(as_of_time, apply_as_of_time)


class VersionedForeignKey(ForeignKey):
    """
    We need to replace the standard ForeignKey declaration in order to be able to introduce
    the VersionedReverseSingleRelatedObjectDescriptor, which allows to go back in time...
    We also default to using the VersionedManyToOneRel, which helps us correctly limit results
    when joining tables via foreign and many-to-many relation fields.
    """
    def __init__(self, to, rel_class=ManyToOneRel, **kwargs):
        super(VersionedForeignKey, self).__init__(to, rel_class=VersionedManyToOneRel, **kwargs)
        self.set_as_of(None, False)

    def set_as_of(self, as_of_time, apply_as_of_time):
        """
        Set the as_of time that will be used to restrict the query for the valid objects.
        :param DateTime as_of_time: Datetime or None (None means use the current objects)
        :param bool apply_as_of_time: If false, then the query will not be restricted by as_of_time
        :return:
        """
        self.as_of_time = as_of_time
        self.apply_as_of_time = apply_as_of_time

    def contribute_to_class(self, cls, name, virtual_only=False):
        super(VersionedForeignKey, self).contribute_to_class(cls, name, virtual_only)
        setattr(cls, self.name, VersionedReverseSingleRelatedObjectDescriptor(self))

    def contribute_to_related_class(self, cls, related):
        """
        Override ForeignKey's methods, and replace the descriptor, if set by the parent's methods
        """
        # Internal FK's - i.e., those with a related name ending with '+' -
        # and swapped models don't get a related descriptor.
        super(VersionedForeignKey, self).contribute_to_related_class(cls, related)
        accessor_name = related.get_accessor_name()
        if hasattr(cls, accessor_name):
            setattr(cls, accessor_name, VersionedForeignRelatedObjectsDescriptor(related))

    def get_extra_restriction(self, where_class, alias, remote_alias):
        """
        Overrides ForeignObject's get_extra_restriction function that returns an SQL statement which is appended to a
        JOIN's conditional filtering part

        :return: SQL conditional statement
        :rtype: str
        """
        cond = None
        if self.apply_as_of_time:
            if self.as_of_time:
                sql = '''{alias}.version_start_date <= %s
                         AND ({alias}.version_end_date > %s OR {alias}.version_end_date is NULL )'''.format(alias=remote_alias)
                params = [self.as_of_time, self.as_of_time]
            else:
                sql = '''{alias}.version_end_date is NULL'''.format(alias=remote_alias)
                params = None
            cond = ExtraWhere([sql], params)
        return cond

class VersionedManyToManyField(ManyToManyField):
    def __init__(self, *args, **kwargs):
        super(VersionedManyToManyField, self).__init__(*args, **kwargs)

    def contribute_to_class(self, cls, name):
        """
        Called at class type creation. So, this method is called, when metaclasses get created
        """
        # self.rel.through needs to be set prior to calling super, since super(...).contribute_to_class refers to it.
        # Classes pointed to by a string do not need to be resolved here, since Django does that at a later point in
        # time - which is nice... ;)
        #
        # Superclasses take care of:
        # - creating the through class if unset
        # - resolving the through class if it's a string
        # - resolving string references within the through class
        if not self.rel.through and not cls._meta.abstract and not cls._meta.swapped:
            self.rel.through = VersionedManyToManyField.create_versioned_many_to_many_intermediary_model(self, cls,
                                                                                                         name)
        super(VersionedManyToManyField, self).contribute_to_class(cls, name)

        # Overwrite the descriptor
        if hasattr(cls, self.name):
            setattr(cls, self.name, VersionedReverseManyRelatedObjectsDescriptor(self))

    def contribute_to_related_class(self, cls, related):
        """
        Called at class type creation. So, this method is called, when metaclasses get created
        """
        super(VersionedManyToManyField, self).contribute_to_related_class(cls, related)
        accessor_name = related.get_accessor_name()
        if hasattr(cls, accessor_name):
            descriptor = VersionedManyRelatedObjectsDescriptor(related, accessor_name)
            setattr(cls, accessor_name, descriptor)
            if hasattr(cls._meta, 'many_to_many_related') and isinstance(cls._meta.many_to_many_related, list):
                cls._meta.many_to_many_related.append(descriptor)
            else:
                cls._meta.many_to_many_related = [descriptor]

    @staticmethod
    def create_versioned_many_to_many_intermediary_model(field, cls, field_name):
        # Let's not care too much on what flags could potentially be set on that intermediary class (e.g. managed, etc)
        # Let's play the game, as if the programmer had specified a class within his models... Here's how.
        # TODO: Test references to 'self'

        from_ = cls._meta.model_name
        to = field.rel.to

        # Force 'to' to be a string (and leave the hard work to Django)
        if not isinstance(field.rel.to, six.string_types):
            to = field.rel.to._meta.object_name
        name = '%s_%s' % (from_, field_name)

        # Since Django 1.7, a migration mechanism is shipped by default with Django. This migration module loads all
        # declared apps' models inside a __fake__ module.
        # This means that the models can be already loaded and registered by their original module, when we
        # reach this point of the application and therefore there is no need to load them a second time.
        if VERSION[:2] >= (1, 7) and cls.__module__ == '__fake__':
            try:
                # Check the apps for an already registered model
                return apps.get_registered_model(cls._meta.app_label, str(name))
            except KeyError:
                # The model has not been registered yet, so continue
                pass

        meta = type('Meta', (object,), {
            # 'unique_together': (from_, to),
            'auto_created': cls,
            'app_label': cls._meta.app_label,
        })
        return type(str(name), (Versionable,), {
            'Meta': meta,
            '__module__': cls.__module__,
            from_: VersionedForeignKey(cls, related_name='%s+' % name),
            to: VersionedForeignKey(to, related_name='%s+' % name),
        })


class VersionedReverseSingleRelatedObjectDescriptor(ReverseSingleRelatedObjectDescriptor):
    """
    A ReverseSingleRelatedObjectDescriptor-typed object gets inserted, when a ForeignKey
    is defined in a Django model. This is one part of the analogue for versioned items.

    Unfortunately, we need to run two queries. The first query satisfies the foreign key
    constraint. After extracting the identity information and combining it with the datetime-
    stamp, we are able to fetch the historic element.
    """

    def __get__(self, instance, instance_type=None):
        """
        The getter method returns the object, which points instance, e.g. choice.poll returns
        a Poll instance, whereas the Poll class defines the ForeignKey.
        :param instance: The object on which the property was accessed
        :param instance_type: The type of the instance object
        :return: Returns a Versionable
        """
        current_elt = super(VersionedReverseSingleRelatedObjectDescriptor, self).__get__(instance, instance_type)

        if not current_elt:
            return None

        if not isinstance(current_elt, Versionable):
            raise TypeError("It seems like " + str(type(self)) + " is not a Versionable")

        return current_elt.__class__.objects.as_of(instance.as_of).get(identity=current_elt.identity)


class VersionedForeignRelatedObjectsDescriptor(ForeignRelatedObjectsDescriptor):
    """
    This descriptor generates the manager class that is used on the related object of a ForeignKey relation
    """

    @cached_property
    def related_manager_cls(self):
        # return create_versioned_related_manager
        manager_cls = super(VersionedForeignRelatedObjectsDescriptor, self).related_manager_cls
        rel_field = self.related.field

        class VersionedRelatedManager(manager_cls):
            def __init__(self, instance):
                super(VersionedRelatedManager, self).__init__(instance)

                # This is a hack, in order to get the versioned related objects
                for key in self.core_filters.keys():
                    if '__exact' in key:
                        self.core_filters[key] = instance.identity

            def get_queryset(self):
                queryset = super(VersionedRelatedManager, self).get_queryset()
                if self.instance.as_of is not None:
                    queryset = queryset.as_of(self.instance.as_of)
                return queryset

            def add(self, *objs):
                cloned_objs = ()
                for obj in objs:
                    if not isinstance(obj, Versionable):
                        raise TypeError("Trying to add a non-Versionable to a VersionedForeignKey relationship")
                    cloned_objs += (obj.clone(),)
                super(VersionedRelatedManager, self).add(*cloned_objs)

            if 'remove' in dir(manager_cls):
                def remove(self, *objs):
                    val = rel_field.get_foreign_related_value(self.instance)
                    cloned_objs = ()
                    for obj in objs:
                        # Is obj actually part of this descriptor set? Otherwise, silently go over it, since Django
                        # handles that case
                        if rel_field.get_local_related_value(obj) == val:
                            # Silently pass over non-versionable items
                            if not isinstance(obj, Versionable):
                                raise TypeError(
                                    "Trying to remove a non-Versionable from a VersionedForeignKey realtionship")
                            cloned_objs += (obj.clone(),)
                    super(VersionedRelatedManager, self).remove(*cloned_objs)

        return VersionedRelatedManager


def create_versioned_many_related_manager(superclass, rel):
    """
    The "casting" which is done in this method is needed, since otherwise, the methods introduced by
    Versionable are not taken into account.
    :param superclass: This is usually a models.Manager
    :param rel: Contains the ManyToMany relation
    :return: A subclass of ManyRelatedManager and Versionable
    """
    many_related_manager_klass = create_many_related_manager(superclass, rel)

    class VersionedManyRelatedManager(many_related_manager_klass):
        def __init__(self, *args, **kwargs):
            super(VersionedManyRelatedManager, self).__init__(*args, **kwargs)
            # Additional core filters are: version_start_date <= t & (version_end_date > t | version_end_date IS NULL)
            # but we cannot work with the Django core filters, since they don't support ORing filters, which
            # is a thing we need to consider the "version_end_date IS NULL" case;
            # So, we define our own set of core filters being applied when versioning
            try:
                version_start_date_field = self.through._meta.get_field('version_start_date')
                version_end_date_field = self.through._meta.get_field('version_end_date')
            except (FieldDoesNotExist) as e:
                print(str(e) + "; available fields are " + ", ".join(self.through._meta.get_all_field_names()))
                raise e
                # FIXME: this probably does not work when auto-referencing

        def get_queryset(self):
            """
            Add a filter to the queryset, limiting the results to be pointed by relationship that are
            valid for the given timestamp (which is taken at the current instance, or set to now, if not
            available).
            Long story short, apply the temporal validity filter also to the intermediary model.
            """

            queryset = super(VersionedManyRelatedManager, self).get_queryset()
            return queryset.as_of(self.instance.as_of)

        def _remove_items(self, source_field_name, target_field_name, *objs):
            """
            Instead of removing items, we simply set the version_end_date of the current item to the
            current timestamp --> t[now].
            Like that, there is no more current entry having that identity - which is equal to
            not existing for timestamps greater than t[now].
            """
            return self._remove_items_at(None, source_field_name, target_field_name, *objs)

        def _remove_items_at(self, timestamp, source_field_name, target_field_name, *objs):
            if objs:
                if timestamp is None:
                    timestamp = get_utc_now()
                old_ids = set()
                for obj in objs:
                    if isinstance(obj, self.model):
                        # The Django 1.7-way is preferred
                        if hasattr(self, 'target_field'):
                            fk_val = self.target_field.get_foreign_related_value(obj)[0]
                        # But the Django 1.6.x -way is supported for backward compatibility
                        elif hasattr(self, '_get_fk_val'):
                            fk_val = self._get_fk_val(obj, target_field_name)
                        else:
                            raise TypeError("We couldn't find the value of the foreign key, this might be due to the "
                                            "use of an unsupported version of Django")
                        old_ids.add(fk_val)
                    else:
                        old_ids.add(obj)
                db = router.db_for_write(self.through, instance=self.instance)
                qs = self.through._default_manager.using(db).filter(**{
                    source_field_name: self.instance.id,
                    '%s__in' % target_field_name: old_ids
                }).as_of(timestamp)
                for relation in qs:
                    relation._delete_at(timestamp)

        if 'add' in dir(many_related_manager_klass):
            def add(self, *objs):
                if not self.instance.is_current:
                    raise SuspiciousOperation(
                        "Adding many-to-many related objects is only possible on the current version")
                super(VersionedManyRelatedManager, self).add(*objs)

            def add_at(self, timestamp, *objs):
                """
                This function adds an object at a certain point in time (timestamp)
                """
                # First off, define the new constructor
                def _through_init(self, *args, **kwargs):
                    super(self.__class__, self).__init__(*args, **kwargs)
                    self.version_birth_date = timestamp
                    self.version_start_date = timestamp

                # Through-classes have an empty constructor, so it can easily be overwritten when needed;
                # This is not the default case, so the overwrite only takes place when we "modify the past"
                self.through.__init_backup__ = self.through.__init__
                self.through.__init__ = _through_init

                # Do the add operation
                self.add(*objs)

                # Remove the constructor again (by replacing it with the original empty constructor)
                self.through.__init__ = self.through.__init_backup__
                del self.through.__init_backup__

            add_at.alters_data = True

        if 'remove' in dir(many_related_manager_klass):
            def remove_at(self, timestamp, *objs):
                """
                Performs the act of removing specified relationships at a specified time (timestamp);
                So, not the objects at a given time are removed, but their relationship!
                """
                self._remove_items_at(timestamp, self.source_field_name, self.target_field_name, *objs)

                # For consistency, also handle the symmetrical case
                if self.symmetrical:
                    self._remove_items_at(timestamp, self.target_field_name, self.source_field_name, *objs)

            remove_at.alters_data = True

    return VersionedManyRelatedManager


class VersionedReverseManyRelatedObjectsDescriptor(ReverseManyRelatedObjectsDescriptor):
    """
    Beside having a very long name, this class is useful when it comes to versioning the
    ReverseManyRelatedObjectsDescriptor (huhu!!). The main part is the exposure of the
    'related_manager_cls' property
    """

    def __get__(self, instance, owner=None):
        """
        Reads the property as which this object is figuring; mainly used for debugging purposes
        :param instance: The instance on which the getter was called
        :param owner: no idea... alternatively called 'instance_type by the superclasses
        :return: A VersionedManyRelatedManager object
        """
        return super(VersionedReverseManyRelatedObjectsDescriptor, self).__get__(instance, owner)

    def __set__(self, instance, value):
        """
        Completely overridden to avoid bulk deletion that happens when the parent method calls clear().

        The parent method's logic is basically: clear all in bulk, then add the given objects in bulk.
        Instead, we figure out which ones are being added and removed, and call add and remove for these values.
        This lets us retain the versioning information.

        Since this is a many-to-many relationship, it is assumed here that the django.db.models.deletion.Collector
        logic, that is used in clear(), is not necessary here.  Collector collects related models, e.g. ones that should
        also be deleted because they have a ON CASCADE DELETE relationship to the object, or, in the case of
        "Multi-table inheritance", are parent objects.

        :param instance: The instance on which the getter was called
        :param value: iterable of items to set
        """

        if not instance.is_current:
            raise SuspiciousOperation(
                "Related values can only be directly set on the current version of an object")

        if not self.field.rel.through._meta.auto_created:
            opts = self.field.rel.through._meta
            raise AttributeError(
                "Cannot set values on a ManyToManyField which specifies an intermediary model.  Use %s.%s's Manager instead." % (
                    opts.app_label, opts.object_name))

        manager = self.__get__(instance)
        # Below comment is from parent __set__ method.  We'll force evaluation, too:
        # clear() can change expected output of 'value' queryset, we force evaluation
        # of queryset before clear; ticket #19816
        value = tuple(value)

        being_removed, being_added = self.get_current_m2m_diff(instance, value)
        timestamp = get_utc_now()
        manager.remove_at(timestamp, *being_removed)
        manager.add_at(timestamp, *being_added)

    def get_current_m2m_diff(self, instance, new_objects):
        """
        :param instance: Versionable object
        :param new_objects: objects which are about to be associated with instance
        :return: (being_removed id list, being_added id list)
        :rtype : tuple
        """
        new_ids = self.pks_from_objects(new_objects)
        relation_manager = self.__get__(instance)

        filter = Q(**{relation_manager.source_field.attname: instance.pk})
        qs = self.through.objects.current.filter(filter)
        try:
            # Django 1.7
            target_name = relation_manager.target_field.attname
        except AttributeError:
            # Django 1.6
            target_name = relation_manager.through._meta.get_field_by_name(
                relation_manager.target_field_name)[0].attname
        current_ids = set(qs.values_list(target_name, flat=True))

        being_removed = current_ids - new_ids
        being_added = new_ids - current_ids
        return list(being_removed), list(being_added)

    def pks_from_objects(self, objects):
        """
        Extract all the primary key strings from the given objects.  Objects may be Versionables, or bare primary keys.
        :rtype : set
        """
        return {o.pk if isinstance(o, Model) else o for o in objects}

    @cached_property
    def related_manager_cls(self):
        return create_versioned_many_related_manager(
            self.field.rel.to._default_manager.__class__,
            self.field.rel
        )


class VersionedManyRelatedObjectsDescriptor(ManyRelatedObjectsDescriptor):
    """
    Beside having a very long name, this class is useful when it comes to versioning the
    ManyRelatedObjectsDescriptor (huhu!!). The main part is the exposure of the
    'related_manager_cls' property
    """

    via_field_name = None

    def __init__(self, related, via_field_name):
        super(VersionedManyRelatedObjectsDescriptor, self).__init__(related)
        self.via_field_name = via_field_name

    def __get__(self, instance, owner=None):
        """
        Reads the property as which this object is figuring; mainly used for debugging purposes
        :param instance: The instance on which the getter was called
        :param owner: no idea... alternatively called 'instance_type by the superclasses
        :return: A VersionedManyRelatedManager object
        """
        return super(VersionedManyRelatedObjectsDescriptor, self).__get__(instance, owner)

    @cached_property
    def related_manager_cls(self):
        return create_versioned_many_related_manager(
            self.related.model._default_manager.__class__,
            self.related.field.rel
        )


class Versionable(models.Model):
    """
    This is pretty much the central point for versioning objects.
    """

    id = models.CharField(max_length=36, primary_key=True)
    """id stands for ID and is the primary key; sometimes also referenced as the surrogate key"""

    identity = models.CharField(max_length=36)
    """identity is used as the identifier of an object, ignoring its versions; sometimes also referenced as the natural key"""

    version_start_date = models.DateTimeField()
    """version_start_date points the moment in time, when a version was created (ie. an versionable was cloned).
    This means, it points the start of a clone's validity period"""

    version_end_date = models.DateTimeField(null=True, default=None, blank=True)
    """version_end_date, if set, points the moment in time, when the entry was duplicated (ie. the entry was cloned). It
    points therefore the end of a clone's validity period"""

    version_birth_date = models.DateTimeField()
    """version_birth_date contains the timestamp pointing to when the versionable has been created (independent of any
    version); This timestamp is bound to an identity"""

    objects = VersionManager()
    """Make the versionable compliant with Django"""

    as_of = None
    """Hold the timestamp at which the object's data was looked up. Its value must always be in between the
    version_start_date and the version_end_date"""

    class Meta:
        abstract = True
        unique_together = ('id', 'identity')

    def delete(self, using=None):
        self._delete_at(get_utc_now(), using)

    def _delete_at(self, timestamp, using=None):
        """
        WARNING: This method is only for internal use, it should not be used
        from outside.

        It is used only in the case when you want to make sure a group of
        related objects are deleted at the exact same time.

        It is certainly not meant to be used for deleting an object and giving it
        a random deletion date of your liking.
        """
        if self.version_end_date is None:
            self.version_end_date = timestamp
            self.save(using=using)
        else:
            raise Exception('Cannot delete anything else but the current version')

    @property
    def is_current(self):
        return self.version_end_date is None

    def _clone_at(self, timestamp):
        """
        WARNING: This method is only for internal use, it should not be used
        from outside.

        This function is mostly intended for testing, to allow creating
        realistic test cases.
        """
        return self.clone(forced_version_date=timestamp)

    def clone(self, forced_version_date=None):
        """
        Clones a Versionable and returns a fresh copy of the original object.
        Original source: ClonableMixin snippet (http://djangosnippets.org/snippets/1271), with the pk/id change
        suggested in the comments

        :param forced_version_date: a timestamp including tzinfo; this value is usually set only internally!
        :return: returns a fresh clone of the original object (with adjusted relations)
        """
        if not self.pk:
            raise ValueError('Instance must be saved before it can be cloned')

        if self.version_end_date:
            raise ValueError('This is a historical item and can not be cloned.')

        if forced_version_date:
            if not self.version_start_date <= forced_version_date <= get_utc_now():
                raise ValueError('The clone date must be between the version start date and now.')
        else:
            forced_version_date = get_utc_now()

        earlier_version = self

        later_version = copy.copy(earlier_version)
        later_version.version_end_date = None
        later_version.version_start_date = forced_version_date

        # set earlier_version's ID to a new UUID so the clone (later_version) can
        # get the old one -- this allows 'head' to always have the original
        # id allowing us to get at all historic foreign key relationships
        earlier_version.id = six.u(str(uuid.uuid4()))
        earlier_version.version_end_date = forced_version_date
        earlier_version.save()

        later_version.save()

        # re-create ManyToMany relations
        for field in earlier_version._meta.many_to_many:
            earlier_version.clone_relations(later_version, field.attname)

        if hasattr(earlier_version._meta, 'many_to_many_related'):
            for rel in earlier_version._meta.many_to_many_related:
                earlier_version.clone_relations(later_version, rel.via_field_name)

        later_version.save()

        return later_version

    def at(self, timestamp):
        """
        Force the create date of an object to be at a certain time; This method can be invoked only on a
        freshly created Versionable object. It must not have been cloned yet. Raises a SuspiciousOperation
        exception, otherwise.
        :param timestamp: a datetime.datetime instance
        """
        # Ensure, it's not a historic item
        if not self.is_current:
            raise SuspiciousOperation(
                "Cannot relocate this Versionable instance in time, since it is a historical item")
        # Ensure it's not a versioned item (that would lead to some ugly situations...
        if not self.version_birth_date == self.version_start_date:
            raise SuspiciousOperation(
                "Cannot relocate this Versionable instance in time, since it is a versioned instance")
        # Ensure the argument is really a timestamp
        if not isinstance(timestamp, datetime.datetime):
            raise ValueError("This is not a datetime.datetime timestamp")
        self.version_birth_date = self.version_start_date = timestamp
        return self

    def clone_relations(self, clone, manager_field_name):
        # Source: the original object, where relations are currently pointing to
        source = getattr(self, manager_field_name)  # returns a VersionedRelatedManager instance
        # Destination: the clone, where the cloned relations should point to
        destination = getattr(clone, manager_field_name)
        for item in source.all():
            destination.add(item)

        # retrieve all current m2m relations pointing the newly created clone
        m2m_rels = source.through.objects.filter(**{source.source_field.attname: clone.id})  # filter for source_id
        for rel in m2m_rels:
            # Only clone the relationship, if it is the current one; Simply adjust the older ones to point the old entry
            # Otherwise, the number of pointers pointing an entry will grow exponentially
            if rel.is_current:
                rel.clone(forced_version_date=self.version_end_date)
            # On rel, set the source ID to self.id
            setattr(rel, source.source_field_name, self)
            rel.save()


class VersionedManyToManyModel(object):
    """
    This class is used for holding signal handlers required for proper versioning
    """

    @staticmethod
    def post_init_initialize(sender, instance, **kwargs):
        """
        This is the signal handler post-initializing the intermediate many-to-many model.
        :param sender: The model class that just had an instance created.
        :param instance: The actual instance of the model that's just been created.
        :param kwargs: Required by Django definition
        :return: None
        """
        if isinstance(instance, sender) and isinstance(instance, Versionable):
            ident = six.u(str(uuid.uuid4()))
            now = get_utc_now()
            if not hasattr(instance, 'version_start_date') or instance.version_start_date is None:
                instance.version_start_date = now
            if not hasattr(instance, 'version_birth_date') or instance.version_birth_date is None:
                instance.version_birth_date = now
            if not hasattr(instance, 'id') or not bool(instance.id):
                instance.id = ident
            if not hasattr(instance, 'identity') or not bool(instance.identity):
                instance.identity = ident


post_init.connect(VersionedManyToManyModel.post_init_initialize)
