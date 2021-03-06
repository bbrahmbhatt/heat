# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Stack endpoint for Heat v1 ReST API.
"""

import itertools
from webob import exc

from heat.api.openstack.v1 import util
from heat.common import wsgi
from heat.common import template_format
from heat.rpc import api as engine_api
from heat.rpc import client as rpc_client
from heat.common import urlfetch

import heat.openstack.common.rpc.common as rpc_common
from heat.openstack.common import log as logging

logger = logging.getLogger(__name__)


class InstantiationData(object):
    """
    The data accompanying a PUT or POST request to create or update a stack.
    """

    PARAMS = (
        PARAM_STACK_NAME,
        PARAM_TEMPLATE,
        PARAM_TEMPLATE_URL,
        PARAM_USER_PARAMS,
    ) = (
        'stack_name',
        'template',
        'template_url',
        'parameters',
    )

    def __init__(self, data):
        """Initialise from the request object."""
        self.data = data

    @staticmethod
    def format_parse(data, data_type):
        """
        Parse the supplied data as JSON or YAML, raising the appropriate
        exception if it is in the wrong format.
        """

        try:
            return template_format.parse(data)
        except ValueError:
            err_reason = _("%s not in valid format") % data_type
            raise exc.HTTPBadRequest(err_reason)

    def stack_name(self):
        """
        Return the stack name.
        """
        if self.PARAM_STACK_NAME not in self.data:
            raise exc.HTTPBadRequest(_("No stack name specified"))
        return self.data[self.PARAM_STACK_NAME]

    def template(self):
        """
        Get template file contents, either inline or from a URL, in JSON
        or YAML format.
        """
        if self.PARAM_TEMPLATE in self.data:
            template_data = self.data[self.PARAM_TEMPLATE]
            if isinstance(template_data, dict):
                return template_data
        elif self.PARAM_TEMPLATE_URL in self.data:
            url = self.data[self.PARAM_TEMPLATE_URL]
            logger.debug('TemplateUrl %s' % url)
            try:
                template_data = urlfetch.get(url)
            except IOError as ex:
                err_reason = _('Could not retrieve template: %s') % str(ex)
                raise exc.HTTPBadRequest(err_reason)
        else:
            raise exc.HTTPBadRequest(_("No template specified"))

        return self.format_parse(template_data, 'Template')

    def user_params(self):
        """
        Get the user-supplied parameters for the stack in JSON format.
        """
        return self.data.get(self.PARAM_USER_PARAMS, {})

    def args(self):
        """
        Get any additional arguments supplied by the user.
        """
        params = self.data.items()
        return dict((k, v) for k, v in params if k not in self.PARAMS)


def format_stack(req, stack, keys=[]):
    include_key = lambda k: k in keys if keys else True

    def transform(key, value):
        if not include_key(key):
            return

        if key == engine_api.STACK_ID:
            yield ('id', value['stack_id'])
            yield ('links', [util.make_link(req, value)])
        else:
            # TODO(zaneb): ensure parameters can be formatted for XML
            #elif key == engine_api.STACK_PARAMETERS:
            #    return key, json.dumps(value)
            yield (key, value)

    return dict(itertools.chain.from_iterable(
        transform(k, v) for k, v in stack.items()))


class StackController(object):
    """
    WSGI controller for stacks resource in Heat v1 API
    Implements the API actions
    """

    def __init__(self, options):
        self.options = options
        self.engine = rpc_client.EngineClient()

    def default(self, req, **args):
        raise exc.HTTPNotFound()

    @util.tenant_local
    def index(self, req):
        """
        Lists summary information for all stacks
        """

        try:
            stacks = self.engine.list_stacks(req.context)
        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        summary_keys = (engine_api.STACK_ID,
                        engine_api.STACK_NAME,
                        engine_api.STACK_DESCRIPTION,
                        engine_api.STACK_STATUS,
                        engine_api.STACK_STATUS_DATA,
                        engine_api.STACK_CREATION_TIME,
                        engine_api.STACK_DELETION_TIME,
                        engine_api.STACK_UPDATED_TIME)

        return {'stacks': [format_stack(req, s, summary_keys) for s in stacks]}

    @util.tenant_local
    def create(self, req, body):
        """
        Create a new stack
        """

        data = InstantiationData(body)

        try:
            result = self.engine.create_stack(req.context,
                                              data.stack_name(),
                                              data.template(),
                                              data.user_params(),
                                              data.args())
        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        if 'Description' in result:
            raise exc.HTTPBadRequest(result['Description'])

        raise exc.HTTPCreated(location=util.make_url(req, result))

    @util.tenant_local
    def lookup(self, req, stack_name, path='', body=None):
        """
        Redirect to the canonical URL for a stack
        """

        try:
            identity = self.engine.identify_stack(req.context,
                                                  stack_name)
        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        location = util.make_url(req, identity)
        if path:
            location = '/'.join([location, path])

        raise exc.HTTPFound(location=location)

    @util.identified_stack
    def show(self, req, identity):
        """
        Gets detailed information for a stack
        """

        try:
            stack_list = self.engine.show_stack(req.context,
                                                identity)
        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        if not stack_list:
            raise exc.HTTPInternalServerError()

        stack = stack_list[0]

        return {'stack': format_stack(req, stack)}

    @util.identified_stack
    def template(self, req, identity):
        """
        Get the template body for an existing stack
        """

        try:
            templ = self.engine.get_template(req.context,
                                             identity)
        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        if templ is None:
            raise exc.HTTPNotFound()

        # TODO(zaneb): always set Content-type to application/json
        return templ

    @util.identified_stack
    def update(self, req, identity, body):
        """
        Update an existing stack with a new template and/or parameters
        """
        data = InstantiationData(body)

        try:
            res = self.engine.update_stack(req.context,
                                           identity,
                                           data.template(),
                                           data.user_params(),
                                           data.args())
        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        if 'Description' in res:
            raise exc.HTTPBadRequest(res['Description'])

        raise exc.HTTPAccepted()

    @util.identified_stack
    def delete(self, req, identity):
        """
        Delete the specified stack
        """

        try:
            res = self.engine.delete_stack(req.context,
                                           identity,
                                           cast=False)

        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        if res is not None:
            raise exc.HTTPBadRequest(res['Error'])

        raise exc.HTTPNoContent()

    @util.tenant_local
    def validate_template(self, req, body):
        """
        Implements the ValidateTemplate API action
        Validates the specified template
        """

        data = InstantiationData(body)

        try:
            result = self.engine.validate_template(req.context,
                                                   data.template())
        except rpc_common.RemoteError as ex:
            return util.remote_error(ex)

        if 'Error' in result:
            raise exc.HTTPBadRequest(result['Error'])

        return result

    @util.tenant_local
    def list_resource_types(self, req):
        """
        Returns a list of valid resource types that may be used in a template.
        """

        try:
            types = self.engine.list_resource_types(req.context)
        except rpc_common.RemoteError as ex:
            raise exc.HTTPInternalServerError(str(ex))

        return {'resource_types': types}


def create_resource(options):
    """
    Stacks resource factory method.
    """
    # TODO(zaneb) handle XML based on Content-type/Accepts
    deserializer = wsgi.JSONRequestDeserializer()
    serializer = wsgi.JSONResponseSerializer()
    return wsgi.Resource(StackController(options), deserializer, serializer)
