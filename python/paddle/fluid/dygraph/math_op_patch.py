#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

from .. import core
from ..framework import Variable, convert_np_dtype_to_dtype_
from ..layers.layer_function_generator import OpProtoHolder
from . import to_variable, no_grad

_supported_int_dtype_ = [
    core.VarDesc.VarType.UINT8,
    core.VarDesc.VarType.INT8,
    core.VarDesc.VarType.INT16,
    core.VarDesc.VarType.INT32,
    core.VarDesc.VarType.INT64,
]


def monkey_patch_math_varbase():
    """
    Similar to monkey_patch_variable.
    The difference is, in dygraph mode, use auto-generated op functions for better performance.
    """

    def safe_get_dtype(var):
        return var.dtype

    @no_grad
    def create_tensor(value, dtype, shape):
        value = float(value)
        inputs = {}
        attrs = {
            'dtype': dtype,
            'shape': shape,
            'value': value,
            'force_cpu': False
        }
        outs = core.ops.fill_constant(inputs, attrs)
        outs['Out'][0].stop_gradient = True
        return outs['Out'][0]

    def create_scalar(value, dtype):
        return create_tensor(value, dtype, shape=[1])

    def astype(self, dtype):
        """
        **Notes**:
            **The variable must be a** :ref:`api_fluid_Tensor`

        Cast a variable to a specified data type.

        Args:

            self(Variable): The source variable

            dtype: The target data type

        Returns:
            Variable: Variable with new dtype

        Examples:
            In Static Graph Mode:

            .. code-block:: python

                import paddle.fluid as fluid

                startup_prog = fluid.Program()
                main_prog = fluid.Program()
                with fluid.program_guard(startup_prog, main_prog):
                    original_variable = fluid.data(name = "new_variable", shape=[2,2], dtype='float32')
                    new_variable = original_variable.astype('int64')
                    print("new var's dtype is: {}".format(new_variable.dtype))

            In Dygraph Mode:

            .. code-block:: python

                import paddle.fluid as fluid
                import numpy as np

                x = np.ones([2, 2], np.float32)
                with fluid.dygraph.guard():
                    original_variable = fluid.dygraph.to_variable(x)
                    print("original var's dtype is: {}, numpy dtype is {}".format(original_variable.dtype, original_variable.numpy().dtype))
                    new_variable = original_variable.astype('int64')
                    print("new var's dtype is: {}, numpy dtype is {}".format(new_variable.dtype, new_variable.numpy().dtype))

        """
        inputs = {'X': [self]}
        attrs = {
            "in_dtype": self.dtype,
            "out_dtype": convert_np_dtype_to_dtype_(dtype)
        }
        outs = core.ops.cast(inputs, attrs)
        return outs['Out'][0]

    def _scalar_elementwise_op_(var, scale, bias):
        inputs = {'X': [var]}
        attrs = {"scale": scale, "bias": bias}
        outs = core.ops.scale(inputs, attrs)
        return outs['Out'][0]

    def _neg_(var):
        return _scalar_elementwise_op_(var, -1.0, 0.0)

    def _scalar_elementwise_add_(var, value):
        return _scalar_elementwise_op_(var, 1.0, value)

    def _scalar_elementwise_sub_(var, value):
        return _scalar_elementwise_op_(var, 1.0, -value)

    def _scalar_elementwise_rsub_(var, value):
        return _scalar_elementwise_op_(var, -1.0, value)

    def _scalar_elementwise_mul_(var, value):
        return _scalar_elementwise_op_(var, value, 0.0)

    def _scalar_elementwise_div_(var, value):
        return _scalar_elementwise_op_(var, 1.0 / value, 0.0)

    def _elemwise_method_creator_(method_name,
                                  op_type,
                                  reverse=False,
                                  scalar_method=None):
        def __impl__(self, other_var):
            # FIXME(zjl): elementwise_div between integers cannot be converted to scale,
            # which may lose accuracy. This is a hot fix for release 1.6.
            if scalar_method is not None and not (
                    op_type == 'elementwise_div' and
                    self.dtype in _supported_int_dtype_):
                if isinstance(other_var, float):
                    if self.dtype in _supported_int_dtype_:
                        assert other_var == int(other_var), \
                            "float value {} cannot convert to integer".format(other_var)
                    return scalar_method(self, other_var)
                elif isinstance(other_var, int):
                    return scalar_method(self, float(other_var))

            lhs_dtype = safe_get_dtype(self)

            if not isinstance(other_var, core.VarBase):
                if reverse:
                    other_var = create_tensor(
                        other_var, dtype=lhs_dtype, shape=self.shape)
                else:
                    # add fill_op 
                    other_var = create_scalar(value=other_var, dtype=lhs_dtype)

            rhs_dtype = safe_get_dtype(other_var)
            if lhs_dtype != rhs_dtype:
                other_var = astype(other_var, lhs_dtype)
            if reverse:
                tmp = self
                self = other_var
                other_var = tmp

            axis = -1
            op = getattr(core.ops, op_type)
            inputs = {'X': [self], 'Y': [other_var]}
            attrs = {'axis': axis}
            outs = op(inputs, attrs)
            return outs['Out'][0]

        comment = OpProtoHolder.instance().get_op_proto(op_type).comment

        __impl__.__doc__ = """
        {0}
        Args:
            self(Variable): left hand variable
            other_var(Variable|float|int): right hand variable

        Returns:
            Variable
        """.format(comment)
        __impl__.__name__ = method_name
        return __impl__

    # inject methods
    for method_name, op_type, reverse, scalar_method in (
        ("__add__", "elementwise_add", False, _scalar_elementwise_add_),
            # a+b == b+a. Do not need to reverse explicitly
        ("__radd__", "elementwise_add", False, _scalar_elementwise_add_),
        ("__sub__", "elementwise_sub", False, _scalar_elementwise_sub_),
        ("__rsub__", "elementwise_sub", True, _scalar_elementwise_rsub_),
        ("__mul__", "elementwise_mul", False, _scalar_elementwise_mul_),
            # a*b == b*a. Do not need to reverse explicitly
        ("__rmul__", "elementwise_mul", False, _scalar_elementwise_mul_),
        ("__div__", "elementwise_div", False, _scalar_elementwise_div_),
        ("__truediv__", "elementwise_div", False, _scalar_elementwise_div_),
        ("__rdiv__", "elementwise_div", True, None),
        ("__rtruediv__", "elementwise_div", True, None),
        ("__pow__", "elementwise_pow", False, None),
        ("__rpow__", "elementwise_pow", True, None),
        ("__floordiv__", "elementwise_floordiv", False, None),
        ("__mod__", "elementwise_mod", False, None),
            # for logical compare
        ("__eq__", "equal", False, None),
        ("__ne__", "not_equal", False, None),
        ("__lt__", "less_than", False, None),
        ("__le__", "less_equal", False, None),
        ("__gt__", "greater_than", False, None),
        ("__ge__", "greater_equal", False, None)):

        setattr(core.VarBase, method_name,
                _elemwise_method_creator_(method_name, op_type, reverse,
                                          scalar_method))

    # b = -a
    core.VarBase.__neg__ = _neg_
    core.VarBase.astype = astype
