#ifndef DEVINFRA_JS_DEBUNDLE_SOLVER_BACKENDS_ORTOOLS_CPSAT_SOLVER_H_
#define DEVINFRA_JS_DEBUNDLE_SOLVER_BACKENDS_ORTOOLS_CPSAT_SOLVER_H_

#include "devinfra/js/debundle/solver_backends/ortools_cpsat/selector_cp_sat.pb.h"

namespace ducktape::debundle::solver_backends::ortools_cpsat {

SelectorCpSatResponse SolveSelectorCpSat(const SelectorCpSatRequest& request);

}  // namespace ducktape::debundle::solver_backends::ortools_cpsat

#endif  // DEVINFRA_JS_DEBUNDLE_SOLVER_BACKENDS_ORTOOLS_CPSAT_SOLVER_H_
