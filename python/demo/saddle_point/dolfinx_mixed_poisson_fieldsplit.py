from mpi4py import MPI
from petsc4py import PETSc

import numpy as np

from basix.ufl import element, mixed_element

import basix
import ufl
import dolfinx
from dolfinx.fem.petsc import LinearProblem

# Import mesh in dolfinx
# Boundary markers: x=1 is 22, x=0 is 30, y=1 is 26, y=0 is 18, z=1 is 31, z=0 is 1
gdim = 3
gmsh_model_rank = 0
mesh_comm = MPI.COMM_WORLD

nx, ny, nz = 20, 20, 20
mesh = dolfinx.mesh.create_box(MPI.COMM_WORLD,
                               [[0.0, 0.0, 0.0],
                                [1., 1., 1.]],
                               [nx, ny, nz],
                               dolfinx.mesh.CellType.tetrahedron)

def z_0(x):
    return np.isclose(x[2], 0)

def y_0(x):
    return np.isclose(x[1], 0)

def x_0(x):
    return np.isclose(x[0], 0)

fdim = mesh.topology.dim - 1
z_0_facets = dolfinx.mesh.locate_entities_boundary(mesh, fdim, z_0)
y_0_facets = dolfinx.mesh.locate_entities_boundary(mesh, fdim, y_0)
x_0_facets = dolfinx.mesh.locate_entities_boundary(mesh, fdim, x_0)

# Concatenate and sort the arrays based on facet indices. Left facets marked with 1, right facets with two
marked_facets = np.hstack([z_0_facets, y_0_facets, x_0_facets])
marked_values = np.hstack([np.full_like(z_0_facets, 1),
                           np.full_like(y_0_facets, 18),
                           np.full_like(x_0_facets, 30)])
sorted_facets = np.argsort(marked_facets)
boundaries = dolfinx.mesh.meshtags(mesh, fdim,
                                   marked_facets[sorted_facets],
                                   marked_values[sorted_facets])

mu = np.array([-2., 0.5, 0.5, 0.5, 3.])

k = 1
Q_el = basix.ufl.element("RT", mesh.basix_cell(), k)
P_el = basix.ufl.element("DG", mesh.basix_cell(), k - 1)
V_el = basix.ufl.mixed_element([Q_el, P_el])
V = dolfinx.fem.FunctionSpace(mesh, V_el)

(sigma, u) = ufl.TrialFunctions(V)
(tau, v) = ufl.TestFunctions(V)

x = ufl.SpatialCoordinate(mesh)
f = 10. * ufl.exp(-mu[0] * ((x[0] - mu[1]) * (x[0] - mu[1]) +
                            (x[1] - mu[2]) * (x[1] - mu[2]) +
                            (x[2] - mu[3]) * (x[2] - mu[3])))

dx = ufl.dx

a = ufl.inner(sigma, tau) * dx + \
    ufl.inner(u, ufl.div(tau)) * dx + \
        ufl.inner(ufl.div(sigma), v) * dx
L = -ufl.inner(f, v) * dx

# Get subspace of V
V0 = V.sub(0)
Q, VQ_map = V0.collapse()
V1 = V.sub(1)
W, VW_map = V1.collapse()

dofs_x0 = dolfinx.fem.locate_dofs_topological((V0, Q),
                                              gdim-1,
                                              boundaries.find(30))

def f1(x):
    values = np.zeros((3, x.shape[1]))
    values[0, :] = np.sin(mu[4] * x[0])
    return values

f_h1 = dolfinx.fem.Function(Q)
f_h1.interpolate(f1)
bc_x0 = dolfinx.fem.dirichletbc(f_h1, dofs_x0, V0)

dofs_y0 = dolfinx.fem.locate_dofs_topological((V0, Q),
                                              gdim-1,
                                              boundaries.find(18))

def f2(x):
    values = np.zeros((3, x.shape[1]))
    values[1, :] = np.sin(mu[4] * x[1])
    return values

f_h2 = dolfinx.fem.Function(Q)
f_h2.interpolate(f2)
bc_y0 = dolfinx.fem.dirichletbc(f_h2, dofs_y0, V0)

dofs_z0 = dolfinx.fem.locate_dofs_topological((V0, Q),
                                              gdim-1,
                                              boundaries.find(1))

def f3(x):
    values = np.zeros((3, x.shape[1]))
    values[2, :] = np.sin(mu[4] * x[2])
    return values

f_h3 = dolfinx.fem.Function(Q)
f_h3.interpolate(f3)
bc_z0 = dolfinx.fem.dirichletbc(f_h3, dofs_z0, V0)

# NOTE
bcs = [bc_x0, bc_y0, bc_z0]

a_cpp = dolfinx.fem.form(a)
l_cpp = dolfinx.fem.form(L)
A = dolfinx.fem.petsc.assemble_matrix(a_cpp, bcs=bcs)
A.assemble()
L = dolfinx.fem.petsc.assemble_vector(l_cpp)
dolfinx.fem.petsc.apply_lifting(L, [a_cpp], [bcs])
L.ghostUpdate(addv=PETSc.InsertMode.ADD,
              mode=PETSc.ScatterMode.REVERSE)
dolfinx.fem.petsc.set_bc(L, bcs)

# Solver setup
ksp = PETSc.KSP()
ksp.create(mesh.comm)
ksp.setOperators(A)
ksp.setType("fgmres")
pc = ksp.getPC()
pc.setType("fieldsplit")
# NOTE see https://petsc.org/release/petsc4py/reference/petsc4py.PETSc.PC.CompositeType.html
pc.setFieldSplitType(PETSc.PC.CompositeType.SCHUR)
# CAUTION it is "assumed" that 1 means full see https://petsc.org/release/petsc4py/reference/petsc4py.PETSc.PC.FieldSplitSchurFactType.html #petsc4py.PETSc.PC.FieldSplitSchurFactType
pc.setFieldSplitSchurFactType(1)
# CAUTION it is "assumed" that 3 means selfp see https://web.cels.anl.gov/projects/petsc/vault/petsc-3.20/docs/petsc4py/reference/petsc4py.PETSc.PC.FieldSplitSchurPreType.html
pc.setFieldSplitSchurPreType(3)
# ksp.getPC().setFactorSolverType("mumps")

# NOTE Since setFieldSplitIS for ISq is called zero-th and for ISw is called first --> subksps[0] corressponds to ISq and subksps[1] corressponds to ISw
ISq = PETSc.IS().createGeneral(VQ_map, mesh.comm)
ISw = PETSc.IS().createGeneral(VW_map, mesh.comm)
pc.setFieldSplitIS(("sigma", ISq), ("u", ISw))


'''
Q_map = Q.dofmap.index_map
W_map = W.dofmap.index_map
offset_q = Q_map.local_range[0] * Q.dofmap.index_map_bs + W_map.local_range[0]
offset_w = Q_map.local_range[0] * Q.dofmap.index_map_bs + offset_q
is_q = PETSc.IS().createStride(Q_map.size_local * Q.dofmap.index_map_bs, offset_q, 1, comm=PETSc.COMM_SELF)
is_w = PETSc.IS().createStride(W_map.size_local, offset_w, 1, comm=PETSc.COMM_SELF)

print(offset_q, Q_map.local_range, Q.dofmap.index_map.size_local, Q.dofmap.index_map_bs, W_map.local_range)
print(offset_w, W_map.local_range, W.dofmap.index_map_bs, Q_map.local_range)
print(is_q, is_q, is_w, is_w)
ksp.getPC().setFieldSplitIS(("sigma", is_q), ("u", is_w))
'''

pc.setUp()

subksps = pc.getFieldSplitSubKSP()
print(subksps)
subksps[0].setType("cg")
subksps[0].getPC().setType("ilu")
subksps[0].rtol = 1.e-12
subksps[1].setType("cg")
subksps[1].getPC().setType("none")
subksps[1].rtol = 1.e-12
ksp.rtol = 1.e-8 # NOTE or ksp.setTolerances(1e-8) # rtol is first argument of setTolerances

# ksp.setConvergenceHistory()
ksp.setFromOptions()
w_h = dolfinx.fem.Function(V)
ksp.solve(L, w_h.vector)
print(f"Number of iterations: {ksp.getIterationNumber()}")
print(f"Convergence reason: {ksp.getConvergedReason()}")
# print(f"Convergence history: {ksp.getConvergenceHistory()}")
A.destroy()
L.destroy()
ksp.destroy()
w_h.x.scatter_forward()
sigma_h, u_h = w_h.split()
sigma_h = sigma_h.collapse()
u_h = u_h.collapse()

with dolfinx.io.XDMFFile(mesh.comm, "out_mixed_poisson/sigma.xdmf", "w") as sol_file:
    sol_file.write_mesh(mesh)
    sol_file.write_function(sigma_h)

with dolfinx.io.XDMFFile(mesh.comm, "out_mixed_poisson/u.xdmf", "w") as sol_file:
    sol_file.write_mesh(mesh)
    sol_file.write_function(u_h)

sigma_norm = mesh.comm.allreduce(dolfinx.fem.assemble_scalar
                                 (dolfinx.fem.form(ufl.inner(sigma_h, sigma_h) *
                                                   dx +
                                                   ufl.inner(ufl.div(sigma_h),
                                                             ufl.div(sigma_h)) *
                                                             dx)), op=MPI.SUM)
u_norm = mesh.comm.allreduce(dolfinx.fem.assemble_scalar
                             (dolfinx.fem.form(ufl.inner(u_h, u_h) *
                                               dx)), op=MPI.SUM)

print(f"sigma norm: {sigma_norm}, u norm: {u_norm}")

# NOTE references
# https://petsc.org/release/petsc4py/reference/petsc4py.PETSc.PC.FieldSplitSchurPreType.html#petsc4py.PETSc.PC.FieldSplitSchurPreType.SELFP
# https://fenicsproject.org/qa/5287/using-the-petsc-pcfieldsplit-in-fenics/
# https://fenicsproject.discourse.group/t/robustness-issue-two-the-same-runs-behave-differently/14347/3
# https://petsc.org/main/manualpages/PC/PCFieldSplitSetIS/
# https://gitlab.com/rafinex-external-rifle/fenicsx-pctools
