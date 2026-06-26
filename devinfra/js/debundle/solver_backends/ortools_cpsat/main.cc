#include <iostream>

#include "absl/log/check.h"
#include "devinfra/js/debundle/solver_backends/ortools_cpsat/selector_cp_sat.pb.h"
#include "devinfra/js/debundle/solver_backends/ortools_cpsat/solver.h"

namespace cpsat = ducktape::debundle::solver_backends::ortools_cpsat;

int main() {
  cpsat::SelectorCpSatRequest request;
  CHECK(request.ParseFromIstream(&std::cin))
      << "failed to parse SelectorCpSatRequest from stdin";
  const cpsat::SelectorCpSatResponse response =
      cpsat::SolveSelectorCpSat(request);
  CHECK(response.SerializeToOstream(&std::cout))
      << "failed to write SelectorCpSatResponse to stdout";
  return 0;
}
