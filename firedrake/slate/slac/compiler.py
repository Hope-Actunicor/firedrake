"""This is Slate's Linear Algebra Compiler. This module is
responsible for generating C++ kernel functions representing
symbolic linear algebra expressions written in Slate.

This linear algebra compiler uses both Firedrake's form compiler,
the Two-Stage Form Compiler (TSFC) and COFFEE's kernel abstract
syntax tree (AST) optimizer. TSFC provides this compiler with
appropriate kernel functions (in C) for evaluating integral
expressions (finite element variational forms written in UFL).
COFFEE's AST base helps with the construction of code blocks
throughout the kernel returned by: `compile_expression`.

The Eigen C++ library (http://eigen.tuxfamily.org/) is required, as
all low-level numerical linear algebra operations are performed using
this templated function library.
"""
from coffee import base as ast
from ufl import MixedElement

import time
from hashlib import md5
import os.path

from firedrake_citations import Citations
from firedrake.tsfc_interface import SplitKernel, KernelInfo, TSFCKernel
from firedrake.slate.slac.kernel_builder import LocalLoopyKernelBuilder, LocalKernelBuilder
from firedrake.slate.slac.utils import topological_sort, SlateTranslator, merge_loopy
from firedrake import op2
from firedrake.logging import logger
from firedrake.parameters import parameters
from firedrake.utils import ScalarType_c
from ufl.log import GREEN
from gem.utils import groupby
from gem.view_gem_dag import view_gem_dag
from gem import impero_utils

from itertools import chain

from pyop2.utils import get_petsc_dir, as_tuple
from pyop2.datatypes import as_cstr
from pyop2.mpi import COMM_WORLD

import firedrake.slate.slate as slate
import numpy as np
import loopy
import gem
from tsfc.loopy import generate as generate_loopy
from loopy.target.c import CTarget

__all__ = ['compile_expression']


try:
    PETSC_DIR, PETSC_ARCH = get_petsc_dir()
except ValueError:
    PETSC_DIR, = get_petsc_dir()
    PETSC_ARCH = None

EIGEN_INCLUDE_DIR = None
if COMM_WORLD.rank == 0:
    filepath = os.path.join(PETSC_ARCH or PETSC_DIR, "lib", "petsc", "conf", "petscvariables")
    with open(filepath) as file:
        for line in file:
            if line.find("EIGEN_INCLUDE") == 0:
                EIGEN_INCLUDE_DIR = line[18:].rstrip()
                break
    if EIGEN_INCLUDE_DIR is None:
        raise ValueError(""" Could not find Eigen configuration in %s. Did you build PETSc with Eigen?""" % PETSC_ARCH or PETSC_DIR)
EIGEN_INCLUDE_DIR = COMM_WORLD.bcast(EIGEN_INCLUDE_DIR, root=0)

cell_to_facets_dtype = np.dtype(np.int8)


class SlateKernel(TSFCKernel):
    @classmethod
    def _cache_key(cls, expr, tsfc_parameters, coffee):
        return md5((expr.expression_hash
                    + str(sorted(tsfc_parameters.items()))).encode()).hexdigest(), expr.ufl_domains()[0].comm

    def __init__(self, expr, tsfc_parameters, coffee=False):
        # if self._initialized:
            # return
        if coffee:
            self.split_kernel = generate_kernel(expr, tsfc_parameters)
        else:
            self.split_kernel = generate_loopy_kernel(expr, tsfc_parameters)
        self._initialized = True


def compile_expression(slate_expr, tsfc_parameters=None, coffee=False):
    """Takes a Slate expression `slate_expr` and returns the appropriate
    :class:`firedrake.op2.Kernel` object representing the Slate expression.

    :arg slate_expr: a :class:'TensorBase' expression.
    :arg tsfc_parameters: an optional `dict` of form compiler parameters to
        be passed to TSFC during the compilation of ufl forms.

    Returns: A `tuple` containing a `SplitKernel(idx, kinfo)`
    """
    if not isinstance(slate_expr, slate.TensorBase):
        raise ValueError("Expecting a `TensorBase` object, not %s" % type(slate_expr))

    # If the expression has already been symbolically compiled, then
    # simply reuse the produced kernel.
    cache = slate_expr._metakernel_cache
    if tsfc_parameters is None:
        tsfc_parameters = parameters["form_compiler"]
    key = str(sorted(tsfc_parameters.items()))
    try:
        return cache[key]
    except KeyError:
        kernel = SlateKernel(slate_expr, tsfc_parameters, coffee).split_kernel
        return cache.setdefault(key, kernel)


def generate_loopy_kernel(slate_expr, tsfc_parameters=None):
    cpu_time = time.time()
    # TODO: Get PyOP2 to write into mixed dats
    if slate_expr.is_mixed:
        raise NotImplementedError("Compiling mixed slate expressions")

    if len(slate_expr.ufl_domains()) > 1:
        raise NotImplementedError("Multiple domains not implemented.")

    Citations().register("Gibson2018")

    print("COMPILING SLATE", slate_expr)
    # Create a loopy builder for the Slate expression,
    # e.g. contains the loopy kernels coming from TSFC
    builder = LocalLoopyKernelBuilder(expression=slate_expr,
                                      tsfc_parameters=tsfc_parameters)

    print("BUILDER DONE")

    # for b in builder.templated_subkernels:
    #     print('\npre slate-to-gem templated_subkernel ff: ', b)
    # Stage 1: slate to gem....
    gem_expr = slate_to_gem(builder)
    
    # for b in builder.templated_subkernels:
    #     print('\npost slate_to_gem: templated_subkernel ff: ', b)
    
    
    print('\n TEMPS')
    for temp_name, temp_gem in builder.temps.items():
        print(temp_name, temp_gem)

    # Stage 2a: gem to loopy...
    loopy_outer = gem_to_loopy(gem_expr, builder)
    print('\nloopy outer:', loopy_outer)

    # for b in builder.templated_subkernels:
    #     print('\npost slate_to_gem: templated_subkernel ff: ', b)

    # Stage 2b: merge loopys...
    loopy_merged = merge_loopy(loopy_outer, builder.templated_subkernels, builder)  # builder owns the callinstruction
    print("LOOPY KERNEL GLUED")
    # for b in builder.templated_subkernels:
    #     print('\ntemplated_subkernel: ', b)
    # print('builder subekernel:', builder.templated_subkernels)
    print('\n\nloopy_merged:', loopy_merged)

    # Stage 2c: register callables...
    loopy_merged = get_inv_callable(loopy_merged)
    loopy_merged = get_solve_callable(loopy_merged)

    # WORKAROUND: Generate code directly from the loopy kernel here,
    # then attach code as a c-string to the op2kernel
    code = loopy.generate_code_v2(loopy_merged).device_code()
    code.replace('void slate_kernel', 'static void slate_kernel')
    loopykernel = op2.Kernel(code, loopy_merged.name, ldargs=["-llapack"])

    kinfo = KernelInfo(kernel=loopykernel,
                       integral_type="cell",  # slate can only do things as contributions to the cell integrals
                       oriented=builder.needs_cell_orientations,
                       subdomain_id="otherwise",
                       domain_number=0,
                       coefficient_map=tuple(range(len(slate_expr.coefficients()))),
                       needs_cell_facets=builder.needs_cell_facets,
                       pass_layer_arg=builder.needs_mesh_layers,
                       needs_cell_sizes=builder.needs_cell_sizes)

    # Cache the resulting kernel
    idx = tuple([0]*slate_expr.rank)
    logger.info(GREEN % "compile_slate_expression finished in %g seconds.", time.time() - cpu_time)
    print('\n===end of generate loopy kernel')
    return (SplitKernel(idx, kinfo),)


def generate_kernel(slate_expr, tsfc_parameters=None):

    cpu_time = time.time()
    # TODO: Get PyOP2 to write into mixed dats
    if slate_expr.is_mixed:
        raise NotImplementedError("Compiling mixed slate expressions")

    if len(slate_expr.ufl_domains()) > 1:
        raise NotImplementedError("Multiple domains not implemented.")

    Citations().register("Gibson2018")
    # Create a builder for the Slate expression
    builder = LocalKernelBuilder(expression=slate_expr,
                                 tsfc_parameters=tsfc_parameters)

    # Keep track of declared temporaries
    declared_temps = {}
    statements = []

    # Declare terminal tensor temporaries
    terminal_declarations = terminal_temporaries(builder, declared_temps)
    statements.extend(terminal_declarations)

    # Generate assembly calls for tensor assembly
    subkernel_calls = tensor_assembly_calls(builder)
    statements.extend(subkernel_calls)

    # Create coefficient temporaries if necessary
    if builder.coefficient_vecs:
        coefficient_temps = coefficient_temporaries(builder, declared_temps)
        statements.extend(coefficient_temps)

    # Create auxiliary temporaries/expressions (if necessary)
    statements.extend(auxiliary_expressions(builder, declared_temps))

    # Generate the kernel information with complete AST
    kinfo = generate_kernel_ast(builder, statements, declared_temps)

    # Cache the resulting kernel
    idx = tuple([0]*slate_expr.rank)
    logger.info(GREEN % "compile_slate_expression finished in %g seconds.", time.time() - cpu_time)
    return (SplitKernel(idx, kinfo),)


def generate_kernel_ast(builder, statements, declared_temps):
    """Glues together the complete AST for the Slate expression
    contained in the :class:`LocalKernelBuilder`.

    :arg builder: The :class:`LocalKernelBuilder` containing
        all relevant expression information.
    :arg statements: A list of COFFEE objects containing all
        assembly calls and temporary declarations.
    :arg declared_temps: A `dict` containing all previously
        declared temporaries.

    Return: A `KernelInfo` object describing the complete AST.
    """
    slate_expr = builder.expression
    if slate_expr.rank == 0:
        # Scalars are treated as 1x1 MatrixBase objects
        shape = (1,)
    else:
        shape = slate_expr.shape

    # Now we create the result statement by declaring its eigen type and
    # using Eigen::Map to move between Eigen and C data structs.
    statements.append(ast.FlatBlock("/* Map eigen tensor into C struct */\n"))
    result_sym = ast.Symbol("T%d" % len(declared_temps))
    result_data_sym = ast.Symbol("A%d" % len(declared_temps))
    result_type = "Eigen::Map<%s >" % eigen_matrixbase_type(shape)
    result = ast.Decl(ScalarType_c, ast.Symbol(result_data_sym), pointers=[("restrict",)])
    result_statement = ast.FlatBlock("%s %s((%s *)%s);\n" % (result_type,
                                                             result_sym,
                                                             ScalarType_c,
                                                             result_data_sym))
    statements.append(result_statement)

    # Generate the complete c++ string performing the linear algebra operations
    # on Eigen matrices/vectors
    statements.append(ast.FlatBlock("/* Linear algebra expression */\n"))
    cpp_string = ast.FlatBlock(slate_to_cpp(slate_expr, declared_temps))
    statements.append(ast.Incr(result_sym, cpp_string))

    # Generate arguments for the macro kernel
    args = [result, ast.Decl(ScalarType_c, builder.coord_sym,
                             pointers=[("restrict",)],
                             qualifiers=["const"])]

    # Orientation information
    if builder.oriented:
        args.append(ast.Decl("int", builder.cell_orientations_sym,
                             pointers=[("restrict",)],
                             qualifiers=["const"]))

    # Coefficient information
    expr_coeffs = slate_expr.coefficients()
    for c in expr_coeffs:
        args.extend([ast.Decl(ScalarType_c, csym,
                              pointers=[("restrict",)],
                              qualifiers=["const"]) for csym in builder.coefficient(c)])

    # Facet information
    if builder.needs_cell_facets:
        f_sym = builder.cell_facet_sym
        f_arg = ast.Symbol("arg_cell_facets")
        f_dtype = as_cstr(cell_to_facets_dtype)

        # cell_facets is locally a flattened 2-D array. We typecast here so we
        # can access its entries using standard array notation.
        cast = "%s (*%s)[2] = (%s (*)[2])%s;\n" % (f_dtype, f_sym, f_dtype, f_arg)
        statements.insert(0, ast.FlatBlock(cast))
        args.append(ast.Decl(f_dtype, f_arg,
                             pointers=[("restrict",)],
                             qualifiers=["const"]))

    # NOTE: We need to be careful about the ordering here. Mesh layers are
    # added as the final argument to the kernel.
    if builder.needs_mesh_layers:
        args.append(ast.Decl("int", builder.mesh_layer_sym))

    # Cell size information
    if builder.needs_cell_sizes:
        args.append(ast.Decl(ScalarType_c, builder.cell_size_sym,
                             pointers=[("restrict",)],
                             qualifiers=["const"]))

    # Macro kernel
    macro_kernel_name = "pyop2_kernel_compile_slate"
    stmts = ast.Block(statements)
    macro_kernel = ast.FunDecl("void", macro_kernel_name, args,
                               stmts, pred=["static", "inline"])

    # Construct the final ast
    kernel_ast = ast.Node(builder.templated_subkernels + [macro_kernel])

    # Now we wrap up the kernel ast as a PyOP2 kernel and include the
    # Eigen header files
    include_dirs = list(builder.include_dirs)
    include_dirs.append(EIGEN_INCLUDE_DIR)
    op2kernel = op2.Kernel(kernel_ast,
                           macro_kernel_name,
                           cpp=True,
                           include_dirs=include_dirs,
                           headers=['#include <Eigen/Dense>',
                                    '#define restrict __restrict'])

    op2kernel.num_flops = builder.expression_flops + builder.terminal_flops
    # Send back a "TSFC-like" SplitKernel object with an
    # index and KernelInfo
    kinfo = KernelInfo(kernel=op2kernel,
                       integral_type=builder.integral_type,
                       oriented=builder.oriented,
                       subdomain_id="otherwise",
                       domain_number=0,
                       coefficient_map=tuple(range(len(expr_coeffs))),
                       needs_cell_facets=builder.needs_cell_facets,
                       pass_layer_arg=builder.needs_mesh_layers,
                       needs_cell_sizes=builder.needs_cell_sizes)

    return kinfo


def auxiliary_expressions(builder, declared_temps):
    """Generates statements for assigning auxiliary temporaries
    and declaring factorizations for local matrix inverses
    (if the matrix is larger than 4 x 4).

    :arg builder: The :class:`LocalKernelBuilder` containing
        all relevant expression information.
    :arg declared_temps: A `dict` containing all previously
        declared temporaries. This dictionary is updated as
        auxiliary expressions are assigned temporaries.
    """

    # These are either already declared terminals or expressions
    # which do not require an extra temporary/expression
    terminals = (slate.Tensor, slate.AssembledVector,
                 slate.Negative, slate.Transpose)
    statements = []

    sorted_exprs = [exp for exp in topological_sort(builder.expression_dag)
                    if ((builder.ref_counter[exp] > 1 and not isinstance(exp, terminals))
                        or isinstance(exp, slate.Factorization))]

    for exp in sorted_exprs:
        if exp not in declared_temps:
            if isinstance(exp, slate.Factorization):
                t = ast.Symbol("dec%d" % len(declared_temps))
                operand, = exp.operands
                expr = slate_to_cpp(operand, declared_temps)
                tensor_type = eigen_matrixbase_type(shape=exp.shape)
                stmt = "Eigen::%s<%s > %s(%s);\n" % (exp.decomposition,
                                                     tensor_type, t, expr)
                statements.append(stmt)
            else:
                t = ast.Symbol("auxT%d" % len(declared_temps))
                result = slate_to_cpp(exp, declared_temps)
                tensor_type = eigen_matrixbase_type(shape=exp.shape)
                stmt = ast.Decl(tensor_type, t)
                assignment = ast.Assign(t, result)
                statements.extend([stmt, assignment])

            declared_temps[exp] = t

    return statements


def coefficient_temporaries(builder, declared_temps):
    """Generates coefficient temporary statements for assigning
    coefficients to vector temporaries.

    :arg builder: The :class:`LocalKernelBuilder` containing
        all relevant expression information.
    :arg declared_temps: A `dict` keeping track of all declared
        temporaries. This dictionary is updated as coefficients
        are assigned temporaries.

    'AssembledVector's require creating coefficient temporaries to
    store data. The temporaries are created by inspecting the function
    space of the coefficient to compute node and dof extents. The
    coefficient is then assigned values by looping over both the node
    extent and dof extent (double FOR-loop). A double FOR-loop is needed
    for each function space (if the function space is mixed, then a loop
    will be constructed for each component space). The general structure
    of each coefficient loop will be:

         FOR (i1=0; i1<node_extent; i1++):
             FOR (j1=0; j1<dof_extent; j1++):
                 VT0[offset + (dof_extent * i1) + j1] = w_0_0[i1][j1]
                 VT1[offset + (dof_extent * i1) + j1] = w_1_0[i1][j1]
                 .
                 .
                 .

    where wT0, wT1, ... are temporaries for coefficients sharing the
    same node and dof extents. The offset is computed based on whether
    the function space is mixed. The offset is always 0 for non-mixed
    coefficients. If the coefficient is mixed, then the offset is
    incremented by the total number of nodal unknowns associated with
    the component spaces of the mixed space.
    """
    statements = [ast.FlatBlock("/* Coefficient temporaries */\n")]
    j = ast.Symbol("j1")
    loops = [ast.FlatBlock("/* Loops for coefficient temps */\n")]
    for dofs, cinfo_list in builder.coefficient_vecs.items():
        # Collect all coefficients which share the same node/dof extent
        assignments = []
        for cinfo in cinfo_list:
            fs_i = cinfo.space_index
            offset = cinfo.offset_index
            c_shape = cinfo.shape
            vector = cinfo.vector
            function = vector._function
            t = cinfo.local_temp

            if vector not in declared_temps:
                # Declare and initialize coefficient temporary
                c_type = eigen_matrixbase_type(shape=c_shape)
                statements.append(ast.Decl(c_type, t))
                declared_temps[vector] = t

            # Assigning coefficient values into temporary
            coeff_sym = ast.Symbol(builder.coefficient(function)[fs_i],
                                   rank=(j, ))
            index = ast.Sum(offset, j)
            coeff_temp = ast.Symbol(t, rank=(index, ))
            assignments.append(ast.Assign(coeff_temp, coeff_sym))

        # loop over dofs
        loop = ast.For(ast.Decl("unsigned int", j, init=0),
                       ast.Less(j, dofs),
                       ast.Incr(j, 1),
                       assignments)

        loops.append(loop)

    statements.extend(loops)

    return statements


def tensor_assembly_calls(builder):
    """Generates a block of statements for assembling the local
    finite element tensors.

    :arg builder: The :class:`LocalKernelBuilder` containing
        all relevant expression information and assembly calls.
    """
    assembly_calls = builder.assembly_calls
    statements = [ast.FlatBlock("/* Assemble local tensors */\n")]

    # Cell integrals are straightforward. Just splat them out.
    statements.extend(assembly_calls["cell"])

    if builder.needs_cell_facets:
        # The for-loop will have the general structure:
        #
        #    FOR (facet=0; facet<num_facets; facet++):
        #        IF (facet is interior):
        #            *interior calls
        #        ELSE IF (facet is exterior):
        #            *exterior calls
        #
        # If only interior (exterior) facets are present,
        # then only a single IF-statement checking for interior
        # (exterior) facets will be present within the loop. The
        # cell facets are labelled `1` for interior, and `0` for
        # exterior.
        statements.append(ast.FlatBlock("/* Loop over cell facets */\n"))
        int_calls = list(chain(*[assembly_calls[it_type]
                                 for it_type in ("interior_facet",
                                                 "interior_facet_vert")]))
        ext_calls = list(chain(*[assembly_calls[it_type]
                                 for it_type in ("exterior_facet",
                                                 "exterior_facet_vert")]))

        # Generate logical statements for handling exterior/interior facet
        # integrals on subdomains.
        # Currently only facet integrals are supported.
        for sd_type in ("subdomains_exterior_facet", "subdomains_interior_facet"):
            stmts = []
            for sd, sd_calls in groupby(assembly_calls[sd_type], lambda x: x[0]):
                _, calls = zip(*sd_calls)
                if_sd = ast.Eq(ast.Symbol(builder.cell_facet_sym, rank=(builder.it_sym, 1)), sd)
                stmts.append(ast.If(if_sd, (ast.Block(calls, open_scope=True),)))

            if sd_type == "subdomains_exterior_facet":
                ext_calls.extend(stmts)
            if sd_type == "subdomains_interior_facet":
                int_calls.extend(stmts)

        # Compute the number of facets to loop over
        domain = builder.expression.ufl_domain()
        if domain.cell_set._extruded:
            num_facets = domain.ufl_cell()._cells[0].num_facets()
        else:
            num_facets = domain.ufl_cell().num_facets()

        if_ext = ast.Eq(ast.Symbol(builder.cell_facet_sym,
                                   rank=(builder.it_sym, 0)), 0)
        if_int = ast.Eq(ast.Symbol(builder.cell_facet_sym,
                                   rank=(builder.it_sym, 0)), 1)
        body = []
        if ext_calls:
            body.append(ast.If(if_ext, (ast.Block(ext_calls, open_scope=True),)))
        if int_calls:
            body.append(ast.If(if_int, (ast.Block(int_calls, open_scope=True),)))

        statements.append(ast.For(ast.Decl("unsigned int", builder.it_sym, init=0),
                                  ast.Less(builder.it_sym, num_facets),
                                  ast.Incr(builder.it_sym, 1), body))

    if builder.needs_mesh_layers:
        # In the presence of interior horizontal facet calls, an
        # IF-ELIF-ELSE block is generated using the mesh levels
        # as conditions for which calls are needed:
        #
        #    IF (layer == bottom_layer):
        #        *bottom calls
        #    ELSE IF (layer == top_layer):
        #        *top calls
        #    ELSE:
        #        *top calls
        #        *bottom calls
        #
        # Any extruded top or bottom calls for extruded facets are
        # included within the appropriate mesh-level IF-blocks. If
        # no interior horizontal facet calls are present, then
        # standard IF-blocks are generated for exterior top/bottom
        # facet calls when appropriate:
        #
        #    IF (layer == bottom_layer):
        #        *bottom calls
        #
        #    IF (layer == top_layer):
        #        *top calls
        #
        # The mesh level is an integer provided as a macro kernel
        # argument.

        # FIXME: No variable layers assumption
        statements.append(ast.FlatBlock("/* Mesh levels: */\n"))
        num_layers = builder.expression.ufl_domain().topological.layers - 1
        int_top = assembly_calls["interior_facet_horiz_top"]
        int_btm = assembly_calls["interior_facet_horiz_bottom"]
        ext_top = assembly_calls["exterior_facet_top"]
        ext_btm = assembly_calls["exterior_facet_bottom"]

        eq_layer = ast.Eq(builder.mesh_layer_sym, num_layers - 1)
        bottom = ast.Block(int_top + ext_btm, open_scope=True)
        top = ast.Block(int_btm + ext_top, open_scope=True)
        rest = ast.Block(int_btm + int_top, open_scope=True)
        statements.append(ast.If(ast.Eq(builder.mesh_layer_sym, 0),
                                 (bottom, ast.If(eq_layer, (top, rest)))))

    return statements


def terminal_temporaries(builder, declared_temps):
    """Generates statements for assigning auxiliary temporaries
    for nodes in an expression with "high" reference count.
    Expressions which require additional temporaries are provided
    by the :class:`LocalKernelBuilder`.

    :arg builder: The :class:`LocalKernelBuilder` containing
                  all relevant expression information.
    :arg declared_temps: A `dict` keeping track of all declared
                         temporaries. This dictionary is updated
                         as terminal tensors are assigned temporaries.
    """
    statements = [ast.FlatBlock("/* Declare and initialize */\n")]
    for exp in builder.temps:
        t = builder.temps[exp]
        statements.append(ast.Decl(eigen_matrixbase_type(exp.shape), t))
        statements.append(ast.FlatBlock("%s.setZero();\n" % t))
        declared_temps[exp] = t

    return statements


def parenthesize(arg, prec=None, parent=None):
    """Parenthesizes an expression."""
    if prec is None or parent is None or prec >= parent:
        return arg
    return "(%s)" % arg


def slate_to_gem(builder, prec=None):
    """ Method encapsulating stage 1.
    Converts the slate expression dag of the LocalKernelBuilder into a gem expression dag.
    Tensor and assembled vectors are already translated before this pass.
    Their translations are owned by the builder.
    """

    traversed_gem_dag = SlateTranslator(builder).slate_to_gem_translate()
    return list([traversed_gem_dag])


def gem_to_loopy(traversed_gem_expr_dag, builder):
    """ Method encapsulating stage 2.
    Converts the gem expression dag into imperoc first, and then further into loopy.
    Outer_loopy contains loopy for slate.
    """
    # Part A: slate to impero_c

    # Add all tensor temporaries as arguments
    args = []  # loopy args for temporaries (tensors and assembled vectors) and arguments
    for k, v in builder.temps.items():
        arg = builder.gem_loopy_dict[v]
        args.append(arg)

    # Creation of return variables for outer loopy
    shape = builder.shape(builder.expression)
    arg = loopy.GlobalArg("output", shape=shape, dtype="double")
    args.append(arg)
    if (type(builder.expression) == slate.Tensor
            or type(builder.expression) == slate.AssembledVector
            or type(builder.expression) == slate.Block):
        idx = builder.gem_indices[str(builder.expression)+"out"]
    else:
        idx = traversed_gem_expr_dag[0].multiindex
    # ret_vars = [gem.Indexed(gem.Variable("output", shape), idx)]
    ret_vars = [gem.Indexed(gem.StructuredSparseVariable("output", shape), idx)]

    # TODO the global argument generation must be made nicer
    # Maybe this can be done in the builder?
    if len(builder.args_extents) > 0:
        arg = loopy.GlobalArg("coords", shape=(builder.args_extents[builder.coordinates_arg],), dtype="double")
        args.append(arg)
    else:
        arg = loopy.GlobalArg("coords", shape=(builder.expression.shape[0],), dtype="double")
        args.append(arg)

    if builder.needs_cell_orientations:
        args.append(loopy.GlobalArg("orientations", shape=(builder.args_extents[builder.cell_orientations_arg],), dtype=np.int32))

    if builder.needs_cell_sizes:
        args.append(loopy.GlobalArg("cell_sizes", shape=(builder.args_extents[builder.cell_size_arg],), dtype="double"))

    # Add coefficients, where AssembledVectors sit on top to args.
    # The fact that the coefficients need to go into the order of
    # builder.expression.coefficients(), plus mixed coefficients,
    # plus split always generating new functions, makes life a bit harder.
    coeff_shape_list = []
    coeff_function_list = []
    for v in builder.coefficient_vecs.values():
        for coeff_info in v:
            coeff_shape_list.append(coeff_info.shape)
            coeff_function_list.append(coeff_info.vector._function)
    get = 0
    for i, c in enumerate(builder.expression.coefficients()):
        try:
            indices = [i for i, x in enumerate(coeff_function_list) if x == c]
            for func_index in indices:
                arg = loopy.GlobalArg("coeff"+str(get), shape=coeff_shape_list[func_index], dtype="double")
                args.append(arg)
                get += 1
        except ValueError:
            pass
        if indices == []:
            element = c.ufl_element()
            if type(element) == MixedElement:
                for j, c_ in enumerate(c.split()):
                    name = "w_{}_{}".format(i, j)
                    args.append(loopy.GlobalArg(name,
                                shape=builder.args_extents[name],
                                dtype="double"))
            else:
                name = "w_{}".format(i)
                args.append(loopy.GlobalArg(name,
                            shape=builder.args_extents[name],
                            dtype="double"))

    # Arg for is exterior (==0)/interior (==1) facet or not
    if builder.needs_cell_facets:
        args.append(loopy.GlobalArg(builder.cell_facets_arg,
                                    shape=(builder.num_facets, 2),
                                    dtype=np.int8))

    if builder.needs_mesh_layers:
        args.append(loopy.TemporaryVariable("layer", shape=(), dtype=np.int32, address_space=loopy.AddressSpace.GLOBAL))

    # Optionally remove ComponentTensors and/or do IndexSum-Delta cancellation
    print('\ntraversed_gem_dag:', traversed_gem_expr_dag)
    # view_gem_dag(traversed_gem_expr_dag)
    traversed_gem_expr_dag = impero_utils.preprocess_gem(traversed_gem_expr_dag)

    # glue assignments to return variable
    assignments = list(zip(ret_vars, traversed_gem_expr_dag))
    impero_c = impero_utils.compile_gem(assignments, (), remove_zeros=False)
    print('\nimpero_c:', impero_c)

    # Part B: impero_c to loopy
    precision = 12
    loopy_outer = generate_loopy(impero_c, args, precision, "double", "loopy_outer")
    return loopy_outer


# TODO: those should got into firedrake loopy!
# STAGE 2c: register external function calls
# the get_*_callable replaces the according callable
# with the c-function which is defined in the preamble
def get_inv_callable(loopy_merged):
    class INVCallable(loopy.ScalarCallable):
        def __init__(self, name, arg_id_to_dtype=None,
                     arg_id_to_descr=None, name_in_target=None):

            super(INVCallable, self).__init__(name,
                                              arg_id_to_dtype=arg_id_to_dtype,
                                              arg_id_to_descr=arg_id_to_descr)

            self.name = name
            self.name_in_target = name_in_target

        def with_types(self, arg_id_to_dtype, kernel, callables_table):
            for i in range(0, len(arg_id_to_dtype)):
                if i not in arg_id_to_dtype or arg_id_to_dtype[i] is None:
                    # the types provided aren't mature enough to specialize the
                    # callable
                    return (self.copy(arg_id_to_dtype=arg_id_to_dtype),
                            callables_table)

            mat_dtype = arg_id_to_dtype[0].numpy_dtype
            name_in_target = "inverse_"

            from loopy.types import NumpyType
            return (self.copy(name_in_target=name_in_target,
                              arg_id_to_dtype={0: NumpyType(mat_dtype), 1: NumpyType(int)}),
                    callables_table)

        def emit_call_insn(self, insn, target, expression_to_code_mapper):
            assert self.is_ready_for_codegen()

            assert isinstance(insn, loopy.CallInstruction)

            parameters = insn.expression.parameters

            parameters = list(parameters)
            par_dtypes = [self.arg_id_to_dtype[i] for i, _ in enumerate(parameters)]

            parameters.append(insn.assignees[0])
            par_dtypes.append(self.arg_id_to_dtype[0])

            from loopy.expression import dtype_to_type_context
            from pymbolic.mapper.stringifier import PREC_NONE
            from loopy.symbolic import SubArrayRef
            from pymbolic import var

            mat_descr = self.arg_id_to_descr[0]

            arg_c_parameters = [
                expression_to_code_mapper(
                    par,
                    PREC_NONE,
                    dtype_to_type_context(target, par_dtype),
                    par_dtype
                ).expr
                if isinstance(par, SubArrayRef) else
                expression_to_code_mapper(
                    par,
                    PREC_NONE,
                    dtype_to_type_context(target, par_dtype),
                    par_dtype
                ).expr
                for par, par_dtype in zip(parameters, par_dtypes)
            ]
            c_parameters = []
            c_parameters.insert(0, arg_c_parameters[0])  # t1
            c_parameters.insert(1, mat_descr.shape[0])  # n
            return var(self.name_in_target)(*c_parameters), False

        def generate_preambles(self, target):
            assert isinstance(target, CTarget)
            inverse_preamble = """
                #include <string.h>
                #include <stdio.h>
                #include <stdlib.h>
                #ifndef Inverse_HPP
                #define Inverse_HPP
                void inverse_(PetscScalar* A, PetscBLASInt N)
                {
                    PetscBLASInt info;
                    PetscBLASInt* ipiv=(PetscBLASInt*) malloc(N*sizeof(PetscBLASInt));
                    PetscScalar* Awork=(PetscScalar*) malloc(N*N*sizeof(PetscScalar));
                    LAPACKgetrf_(&N,&N,A,&N,ipiv,&info);
                    if(info==0)
                        LAPACKgetri_(&N,A,&N,ipiv,Awork,&N,&info);
                    if(info!=0)
                        fprintf(stderr,\"Getri throws nonzero info.\");
                }
                #endif
            """
            yield("lapack_inverse", "#include <petscsystypes.h>\n#include <petscblaslapack.h>\n"+inverse_preamble)
            return

    def inv_fn_lookup(target, identifier):
        if identifier == 'inv':
            return INVCallable(name='inv')

        return None

    loopy_merged = loopy.register_function_id_to_in_knl_callable_mapper(loopy_merged, inv_fn_lookup)

    return loopy_merged


def get_solve_callable(loopy_merged):
    class SolveCallable(loopy.ScalarCallable):
        def __init__(self, name, arg_id_to_dtype=None,
                     arg_id_to_descr=None, name_in_target=None):

            super(SolveCallable, self).__init__(name,
                                                arg_id_to_dtype=arg_id_to_dtype,
                                                arg_id_to_descr=arg_id_to_descr)

            self.name = name
            self.name_in_target = name_in_target

        def with_types(self, arg_id_to_dtype, kernel, callables_table):
            for i in range(0, len(arg_id_to_dtype)):
                if i not in arg_id_to_dtype or arg_id_to_dtype[i] is None:
                    # the types provided aren't mature enough to specialize the
                    # callable
                    return (self.copy(arg_id_to_dtype=arg_id_to_dtype),
                            callables_table)

            mat_dtype = arg_id_to_dtype[0].numpy_dtype
            name_in_target = "solve_"

            from loopy.types import NumpyType
            return (self.copy(name_in_target=name_in_target,
                              arg_id_to_dtype={0: NumpyType(mat_dtype), 1: NumpyType(mat_dtype), 2: NumpyType(int)}),
                    callables_table)

        def emit_call_insn(self, insn, target, expression_to_code_mapper):
            assert self.is_ready_for_codegen()
            assert isinstance(insn, loopy.CallInstruction)  # for batched this should be call instruction

            parameters = insn.expression.parameters

            if type(parameters) != list:
                parameters = list(parameters)
            par_dtypes = [self.arg_id_to_dtype[i] for i, _ in enumerate(parameters)]  # TODO: get the reads right

            parameters.append(insn.assignees[0])
            par_dtypes.append(self.arg_id_to_dtype[0])

            from loopy.expression import dtype_to_type_context
            from pymbolic.mapper.stringifier import PREC_NONE
            from loopy.symbolic import SubArrayRef
            from pymbolic import var

            mat_descr_A = self.arg_id_to_descr[0]

            arg_c_parameters = [
                expression_to_code_mapper(
                    par,
                    PREC_NONE,
                    dtype_to_type_context(target, par_dtype),
                    par_dtype
                ).expr
                if isinstance(par, SubArrayRef) else
                expression_to_code_mapper(
                    par,
                    PREC_NONE,
                    dtype_to_type_context(target, par_dtype),
                    par_dtype
                ).expr
                for par, par_dtype in zip(parameters, par_dtypes)
            ]
            c_parameters = []
            c_parameters.insert(0, arg_c_parameters[0])  # A
            c_parameters.insert(1, arg_c_parameters[1])  # B
            c_parameters.insert(2, mat_descr_A.shape[1])  # n
            return var(self.name_in_target)(*c_parameters), False

        def generate_preambles(self, target):
            assert isinstance(target, CTarget)
            code = """#include <string.h>
                #include <stdio.h>
                #include <stdlib.h>

                #ifndef Solve_HPP
                #define Solve_HPP

                void solve_(PetscScalar* A, PetscScalar* B, PetscBLASInt N)
                {
                    PetscBLASInt info;
                    PetscBLASInt* ipiv=(PetscBLASInt*) malloc(N*sizeof(PetscBLASInt));
                    PetscBLASInt NRHS;
                    NRHS=1;
                    LAPACKgesv_(&N,&NRHS,A,&N,ipiv,B,&N,&info);
                    if(info!=0)
                        fprintf(stderr,\"Gesv throws nonzero info.\");
                }
                #endif
            """

            yield("lapack_solve", "#include <petscsystypes.h>\n#include <petscblaslapack.h>\n"+code)
            return

    def fac_fn_lookup(target, identifier):
        if identifier == 'solve':
            return SolveCallable(name='solve')

        return None

    loopy_merged = loopy.register_function_id_to_in_knl_callable_mapper(loopy_merged, fac_fn_lookup)

    return loopy_merged


def slate_to_cpp(expr, temps, prec=None):
    """Translates a Slate expression into its equivalent representation in
    the Eigen C++ syntax.

    :arg expr: a :class:`slate.TensorBase` expression.
    :arg temps: a `dict` of temporaries which map a given expression to its
        corresponding representation as a `coffee.Symbol` object.
    :arg prec: an argument dictating the order of precedence in the linear
        algebra operations. This ensures that parentheticals are placed
        appropriately and the order in which linear algebra operations
        are performed are correct.

    Returns:
        a `string` which represents the C/C++ code representation of the
        `slate.TensorBase` expr.
    """
    # If the tensor is terminal, it has already been declared.
    # Coefficients defined as AssembledVectors will have been declared
    # by now, as well as any other nodes with high reference count or
    # matrix factorizations.
    if expr in temps:
        return temps[expr].gencode()

    elif isinstance(expr, slate.Transpose):
        tensor, = expr.operands
        return "(%s).transpose()" % slate_to_cpp(tensor, temps)

    elif isinstance(expr, slate.Inverse):
        tensor, = expr.operands
        return "(%s).inverse()" % slate_to_cpp(tensor, temps)

    elif isinstance(expr, slate.Negative):
        tensor, = expr.operands
        result = "-%s" % slate_to_cpp(tensor, temps, expr.prec)
        return parenthesize(result, expr.prec, prec)

    elif isinstance(expr, (slate.Add, slate.Mul)):
        op = {slate.Add: '+',
              slate.Mul: '*'}[type(expr)]
        A, B = expr.operands
        result = "%s %s %s" % (slate_to_cpp(A, temps, expr.prec),
                               op,
                               slate_to_cpp(B, temps, expr.prec))

        return parenthesize(result, expr.prec, prec)

    elif isinstance(expr, slate.Block):
        tensor, = expr.operands
        indices = expr._indices
        try:
            ridx, cidx = indices
        except ValueError:
            ridx, = indices
            cidx = 0
        rids = as_tuple(ridx)
        cids = as_tuple(cidx)

        # Check if indices are non-contiguous
        if not all(all(ids[i] + 1 == ids[i + 1] for i in range(len(ids) - 1))
                   for ids in (rids, cids)):
            raise NotImplementedError("Non-contiguous blocks not implemented")

        rshape = expr.shape[0]
        rstart = sum(tensor.shapes[0][:min(rids)])
        if expr.rank == 1:
            cshape = 1
            cstart = 0
        else:
            cshape = expr.shape[1]
            cstart = sum(tensor.shapes[1][:min(cids)])

        result = "(%s).block<%d, %d>(%d, %d)" % (slate_to_cpp(tensor,
                                                              temps,
                                                              expr.prec),
                                                 rshape, cshape,
                                                 rstart, cstart)

        return parenthesize(result, expr.prec, prec)

    elif isinstance(expr, slate.Solve):
        A, B = expr.operands
        result = "%s.solve(%s)" % (slate_to_cpp(A, temps, expr.prec),
                                   slate_to_cpp(B, temps, expr.prec))

        return parenthesize(result, expr.prec, prec)

    else:
        raise NotImplementedError("Type %s not supported.", type(expr))


def eigen_matrixbase_type(shape):
    """Returns the Eigen::Matrix declaration of the tensor.

    :arg shape: a tuple of integers the denote the shape of the
        :class:`slate.TensorBase` object.

    Returns:
        a string indicating the appropriate declaration of the
        `slate.TensorBase` object in the appropriate Eigen C++ template
        library syntax.
    """
    if len(shape) == 0:
        rows = 1
        cols = 1
    elif len(shape) == 1:
        rows = shape[0]
        cols = 1
    else:
        if not len(shape) == 2:
            raise NotImplementedError(
                "%d-rank tensors are not supported." % len(shape)
            )
        rows = shape[0]
        cols = shape[1]
    if cols != 1:
        order = ", Eigen::RowMajor"
    else:
        order = ""

    return "Eigen::Matrix<double, %d, %d%s>" % (rows, cols, order)
