#include "devinfra/js/debundle/solver_backends/ortools_cpsat/solver.h"

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstdint>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"
#include "ortools/sat/cp_model.h"
#include "ortools/sat/cp_model.pb.h"
#include "ortools/sat/cp_model_checker.h"
#include "ortools/sat/cp_model_solver.h"
#include "ortools/sat/model.h"

namespace ducktape::debundle::solver_backends::ortools_cpsat {
namespace {

namespace sat = ::operations_research::sat;
using VariableMap = std::map<uint32_t, sat::IntVar>;
using AllowedRowSetMap = std::map<uint32_t, const AllowedRowSet*>;
using SharedSparseDomainMap = std::map<uint32_t, const SharedSparseDomain*>;

constexpr char kNumSearchWorkersEnv[] =
    "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_NUM_SEARCH_WORKERS";
constexpr char kMaxTimeSecondsEnv[] =
    "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS";

struct ProjectionRow {
  std::vector<std::pair<uint32_t, int64_t>> values;

  bool operator<(const ProjectionRow& other) const {
    return values < other.values;
  }
};

struct ProjectionVariable {
  uint32_t id;
  sat::IntVar variable;
};

SelectorCpSatResponse InvalidResponse(const absl::Status& status) {
  SelectorCpSatResponse response;
  response.set_status(SOLVER_STATUS_INVALID);
  response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
  response.set_diagnostic(std::string(status.message()));
  return response;
}

SelectorCpSatResponse InvalidModelResponse(
    const sat::CpSolverResponse& solver_response,
    const sat::CpModelProto& model_proto) {
  SelectorCpSatResponse response;
  response.set_status(SOLVER_STATUS_INVALID);
  response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
  response.set_solver_response_stats(sat::CpSolverResponseStats(solver_response));
  const std::string validation_error = sat::ValidateCpModel(model_proto);
  if (validation_error.empty()) {
    response.set_diagnostic(
        "CP-SAT reported MODEL_INVALID, but ValidateCpModel returned no error");
  } else {
    response.set_diagnostic(validation_error);
  }
  return response;
}

absl::Status MissingVariableStatus(uint32_t variable_id) {
  return absl::InvalidArgumentError(
      absl::StrCat("unknown variable id ", variable_id));
}

std::optional<std::string> EnvValue(const char* name) {
  const char* value = std::getenv(name);
  if (value == nullptr || value[0] == '\0') {
    return std::nullopt;
  }
  return std::string(value);
}

absl::StatusOr<int> ParsePositiveIntEnv(const char* name) {
  const std::optional<std::string> raw = EnvValue(name);
  if (!raw.has_value()) {
    return 0;
  }

  errno = 0;
  char* end = nullptr;
  const long value = std::strtol(raw->c_str(), &end, 10);
  if (errno != 0 || end == raw->c_str() || *end != '\0' || value <= 0 ||
      value > std::numeric_limits<int>::max()) {
    return absl::InvalidArgumentError(
        absl::StrCat(name, " must be a positive integer, got `", *raw, "`"));
  }
  return static_cast<int>(value);
}

absl::StatusOr<double> ParsePositiveDoubleEnv(const char* name) {
  const std::optional<std::string> raw = EnvValue(name);
  if (!raw.has_value()) {
    return 0.0;
  }

  errno = 0;
  char* end = nullptr;
  const double value = std::strtod(raw->c_str(), &end);
  if (errno != 0 || end == raw->c_str() || *end != '\0' || value <= 0.0) {
    return absl::InvalidArgumentError(
        absl::StrCat(name, " must be a positive number, got `", *raw, "`"));
  }
  return value;
}

absl::StatusOr<sat::SatParameters> BuildSatParameters() {
  sat::SatParameters parameters;
  parameters.set_num_search_workers(1);

  absl::StatusOr<int> num_search_workers =
      ParsePositiveIntEnv(kNumSearchWorkersEnv);
  if (!num_search_workers.ok()) {
    return num_search_workers.status();
  }
  if (*num_search_workers != 0) {
    parameters.set_num_search_workers(*num_search_workers);
  }

  absl::StatusOr<double> max_time_seconds =
      ParsePositiveDoubleEnv(kMaxTimeSecondsEnv);
  if (!max_time_seconds.ok()) {
    return max_time_seconds.status();
  }
  if (*max_time_seconds != 0.0) {
    parameters.set_max_time_in_seconds(*max_time_seconds);
  }

  return parameters;
}

absl::StatusOr<SharedSparseDomainMap> BuildSharedSparseDomainMap(
    const SelectorCpSatRequest& request) {
  SharedSparseDomainMap domains;
  for (const SharedSparseDomain& domain : request.shared_sparse_domains()) {
    const auto insert_result = domains.emplace(domain.id(), &domain);
    if (!insert_result.second) {
      return absl::InvalidArgumentError(
          absl::StrCat("duplicate shared sparse domain id ", domain.id()));
    }
  }
  return domains;
}

absl::StatusOr<::operations_research::Domain> DomainFromSparseValues(
    uint32_t variable_id, const google::protobuf::RepeatedField<int64_t>& raw_values) {
  if (raw_values.empty()) {
    return absl::InvalidArgumentError(
        absl::StrCat("variable ", variable_id, " has an empty domain"));
  }
  std::vector<int64_t> values(raw_values.begin(), raw_values.end());
  std::sort(values.begin(), values.end());
  if (std::adjacent_find(values.begin(), values.end()) != values.end()) {
    return absl::InvalidArgumentError(
        absl::StrCat("variable ", variable_id, " has duplicate domain values"));
  }
  return ::operations_research::Domain::FromValues(std::move(values));
}

absl::StatusOr<::operations_research::Domain> DomainForVariable(
    const Variable& variable, const SharedSparseDomainMap& shared_domains) {
  switch (variable.domain_case()) {
    case Variable::kDenseDomain: {
      if (variable.dense_domain().value_count() == 0) {
        return absl::InvalidArgumentError(
            absl::StrCat("variable ", variable.id(), " has an empty domain"));
      }
      return ::operations_research::Domain(
          0, static_cast<int64_t>(variable.dense_domain().value_count()) - 1);
    }
    case Variable::kSparseDomain: {
      return DomainFromSparseValues(variable.id(),
                                    variable.sparse_domain().values());
    }
    case Variable::kSharedSparseDomainId: {
      const auto domain = shared_domains.find(variable.shared_sparse_domain_id());
      if (domain == shared_domains.end()) {
        return absl::InvalidArgumentError(absl::StrCat(
            "variable ", variable.id(),
            " references unknown shared_sparse_domain_id ",
            variable.shared_sparse_domain_id()));
      }
      return DomainFromSparseValues(variable.id(), domain->second->values());
    }
    case Variable::DOMAIN_NOT_SET:
      return absl::InvalidArgumentError(
          absl::StrCat("variable ", variable.id(), " has no domain"));
  }
  return absl::InvalidArgumentError(
      absl::StrCat("variable ", variable.id(), " has unsupported domain"));
}

absl::Status AddVariables(const SelectorCpSatRequest& request,
                          sat::CpModelBuilder* model,
                          VariableMap* variables) {
  absl::StatusOr<SharedSparseDomainMap> shared_domains =
      BuildSharedSparseDomainMap(request);
  if (!shared_domains.ok()) {
    return shared_domains.status();
  }
  for (const Variable& variable : request.variables()) {
    if (variables->find(variable.id()) != variables->end()) {
      return absl::InvalidArgumentError(
          absl::StrCat("duplicate variable id ", variable.id()));
    }

    absl::StatusOr<::operations_research::Domain> domain =
        DomainForVariable(variable, *shared_domains);
    if (!domain.ok()) {
      return domain.status();
    }

    sat::IntVar int_var =
        model->NewIntVar(*domain).WithName(variable.debug_name());
    variables->emplace(variable.id(), int_var);
  }
  return absl::OkStatus();
}

absl::StatusOr<std::vector<sat::IntVar>> LookupVariables(
    const VariableMap& variables,
    const google::protobuf::RepeatedField<uint32_t>& ids) {
  std::vector<sat::IntVar> found;
  found.reserve(ids.size());
  for (uint32_t id : ids) {
    const auto it = variables.find(id);
    if (it == variables.end()) {
      return MissingVariableStatus(id);
    }
    found.push_back(it->second);
  }
  return found;
}

absl::StatusOr<AllowedRowSetMap> BuildAllowedRowSetMap(
    const SelectorCpSatRequest& request) {
  AllowedRowSetMap allowed_row_sets;
  for (const AllowedRowSet& row_set : request.allowed_row_sets()) {
    const auto insert_result = allowed_row_sets.emplace(row_set.id(), &row_set);
    if (!insert_result.second) {
      return absl::InvalidArgumentError(
          absl::StrCat("duplicate allowed row set id ", row_set.id()));
    }
  }
  return allowed_row_sets;
}

absl::Status AddAllowedRows(
    uint32_t table_id, size_t expected_arity,
    const google::protobuf::RepeatedPtrField<Tuple>& rows,
    const std::string& row_source_context, sat::TableConstraint* allowed) {
  for (int row_index = 0; row_index < rows.size(); ++row_index) {
    const Tuple& row = rows.Get(row_index);
    if (static_cast<size_t>(row.values_size()) != expected_arity) {
      return absl::InvalidArgumentError(absl::StrCat(
          "table constraint ", table_id, " row ", row_index, row_source_context,
          " has arity ", row.values_size(), ", expected ", expected_arity));
    }
    const std::vector<int64_t> values(row.values().begin(), row.values().end());
    allowed->AddTuple(values);
  }
  return absl::OkStatus();
}

absl::Status AddAllowedRowSet(uint32_t table_id, size_t expected_arity,
                              const AllowedRowSet& row_set,
                              sat::TableConstraint* allowed) {
  if (row_set.arity() != expected_arity) {
    return absl::InvalidArgumentError(absl::StrCat(
        "table constraint ", table_id, " row_set_id ", row_set.id(),
        " has arity ", row_set.arity(), ", expected ", expected_arity));
  }
  if (row_set.arity() == 0) {
    return absl::InvalidArgumentError(absl::StrCat(
        "table constraint ", table_id, " row_set_id ", row_set.id(),
        " has zero arity"));
  }
  if (row_set.values_size() % row_set.arity() != 0) {
    return absl::InvalidArgumentError(absl::StrCat(
        "table constraint ", table_id, " row_set_id ", row_set.id(),
        " has ", row_set.values_size(), " values, not a multiple of arity ",
        row_set.arity()));
  }

  allowed->MutableProto()->mutable_table()->mutable_values()->MergeFrom(
      row_set.values());
  return absl::OkStatus();
}

absl::Status AddAllowedTables(const SelectorCpSatRequest& request,
                              const VariableMap& variables,
                              sat::CpModelBuilder* model) {
  absl::StatusOr<AllowedRowSetMap> allowed_row_sets =
      BuildAllowedRowSetMap(request);
  if (!allowed_row_sets.ok()) {
    return allowed_row_sets.status();
  }
  for (const TableConstraint& table : request.allowed_tables()) {
    absl::StatusOr<std::vector<sat::IntVar>> table_variables =
        LookupVariables(variables, table.variable_ids());
    if (!table_variables.ok()) {
      return table_variables.status();
    }
    if (table_variables->empty()) {
      return absl::InvalidArgumentError(
          absl::StrCat("table constraint ", table.id(), " has no variables"));
    }

    sat::TableConstraint allowed =
        model->AddAllowedAssignments(*table_variables);
    if (table.has_row_set_id()) {
      const auto row_set = allowed_row_sets->find(table.row_set_id());
      if (row_set == allowed_row_sets->end()) {
        return absl::InvalidArgumentError(absl::StrCat(
            "table constraint ", table.id(), " references unknown row_set_id ",
            table.row_set_id()));
      }
      const absl::Status status =
          AddAllowedRowSet(table.id(), table_variables->size(), *row_set->second,
                           &allowed);
      if (!status.ok()) {
        return status;
      }
    } else {
      const absl::Status status =
          AddAllowedRows(table.id(), table_variables->size(),
                         table.allowed_rows(), "", &allowed);
      if (!status.ok()) {
        return status;
      }
    }
  }
  return absl::OkStatus();
}

absl::Status AddBinaryConstraints(const SelectorCpSatRequest& request,
                                  const VariableMap& variables,
                                  sat::CpModelBuilder* model) {
  for (const BinaryConstraint& constraint : request.binary_constraints()) {
    const auto left = variables.find(constraint.left_variable_id());
    if (left == variables.end()) {
      return MissingVariableStatus(constraint.left_variable_id());
    }
    const auto right = variables.find(constraint.right_variable_id());
    if (right == variables.end()) {
      return MissingVariableStatus(constraint.right_variable_id());
    }

    switch (constraint.kind()) {
      case BINARY_CONSTRAINT_KIND_EQUAL:
        model->AddEquality(left->second, right->second);
        break;
      case BINARY_CONSTRAINT_KIND_NOT_EQUAL:
        model->AddNotEqual(left->second, right->second);
        break;
      case BINARY_CONSTRAINT_KIND_ORDINAL_BEFORE:
        model->AddLessThan(left->second, right->second);
        break;
      case BINARY_CONSTRAINT_KIND_UNSPECIFIED:
      default:
        return absl::InvalidArgumentError(absl::StrCat(
            "unsupported binary constraint kind ",
            static_cast<int>(constraint.kind())));
    }
  }
  return absl::OkStatus();
}

absl::Status AddLinearConstraints(const SelectorCpSatRequest& request,
                                  const VariableMap& variables,
                                  sat::CpModelBuilder* model) {
  for (const LinearConstraint& constraint : request.linear_constraints()) {
    if (constraint.variable_ids_size() == 0) {
      return absl::InvalidArgumentError("linear constraint has no variables");
    }
    if (constraint.variable_ids_size() != constraint.coefficients_size()) {
      return absl::InvalidArgumentError(absl::StrCat(
          "linear constraint has ", constraint.variable_ids_size(),
          " variables but ", constraint.coefficients_size(), " coefficients"));
    }
    if (constraint.domain_size() == 0 || constraint.domain_size() % 2 != 0) {
      return absl::InvalidArgumentError(
          "linear constraint has invalid flat interval domain");
    }
    for (int index = 0; index < constraint.domain_size(); index += 2) {
      if (constraint.domain(index) > constraint.domain(index + 1)) {
        return absl::InvalidArgumentError(
            "linear constraint has inverted domain interval");
      }
    }
    std::vector<sat::IntVar> linear_variables;
    linear_variables.reserve(constraint.variable_ids_size());
    for (uint32_t variable_id : constraint.variable_ids()) {
      const auto variable = variables.find(variable_id);
      if (variable == variables.end()) {
        return MissingVariableStatus(variable_id);
      }
      linear_variables.push_back(variable->second);
    }
    const std::vector<int64_t> coefficients(constraint.coefficients().begin(),
                                            constraint.coefficients().end());
    const std::vector<int64_t> domain(constraint.domain().begin(),
                                      constraint.domain().end());
    const sat::LinearExpr expression =
        sat::LinearExpr::WeightedSum(linear_variables, coefficients) +
        constraint.offset();
    model->AddLinearConstraint(expression,
                               ::operations_research::Domain::FromFlatIntervals(
                                   domain));
  }
  return absl::OkStatus();
}

absl::Status AddAllDifferentConstraints(const SelectorCpSatRequest& request,
                                        const VariableMap& variables,
                                        sat::CpModelBuilder* model) {
  for (const AllDifferent& all_different : request.all_different()) {
    absl::StatusOr<std::vector<sat::IntVar>> all_different_variables =
        LookupVariables(variables, all_different.variable_ids());
    if (!all_different_variables.ok()) {
      return all_different_variables.status();
    }
    if (all_different_variables->size() < 2) {
      return absl::InvalidArgumentError(absl::StrCat(
          "all_different constraint ", all_different.id(),
          " has fewer than two variables"));
    }
    model->AddAllDifferent(*all_different_variables);
  }
  return absl::OkStatus();
}

absl::StatusOr<std::vector<ProjectionVariable>> ProjectionVariables(
    const SelectorCpSatRequest& request, const VariableMap& variables) {
  std::set<uint32_t> ids;
  for (const TargetProjection& projection : request.target_projections()) {
    ids.insert(projection.owner_variable_id());
    if (projection.has_binding_variable_id()) {
      ids.insert(projection.binding_variable_id());
    }
  }
  for (uint32_t id : ids) {
    if (variables.find(id) == variables.end()) {
      return MissingVariableStatus(id);
    }
  }
  std::vector<ProjectionVariable> projection_variables;
  projection_variables.reserve(ids.size());
  for (uint32_t id : ids) {
    projection_variables.push_back({id, variables.at(id)});
  }
  return projection_variables;
}

ProjectionRow ProjectionRowFromSolution(
    const sat::CpSolverResponse& response,
    const std::vector<ProjectionVariable>& projection_variables) {
  ProjectionRow row;
  row.values.reserve(projection_variables.size());
  for (const ProjectionVariable& projection_variable : projection_variables) {
    row.values.push_back(
        {projection_variable.id,
         sat::SolutionIntegerValue(response, projection_variable.variable)});
  }
  return row;
}

void AddRows(const std::set<ProjectionRow>& rows,
             SelectorCpSatResponse* response) {
  for (const ProjectionRow& row : rows) {
    AssignmentRow* assignment_row = response->add_assignments();
    for (const auto& [variable_id, value] : row.values) {
      Assignment* assignment = assignment_row->add_values();
      assignment->set_variable_id(variable_id);
      assignment->set_value(value);
    }
  }
}

// Collects projected rows until every projection variable is either proven
// fixed or has its alternative values listed (up to a per-variable cap).
// Full enumeration of projected rows is exponential on ambiguous programs;
// this needs at most (#ambiguous variables + 1) solves to prove which variables
// are fixed, plus at most `max_alternatives` solves per ambiguous variable.
class SupportSearch {
 public:
  SupportSearch(const sat::CpModelBuilder& base_model,
                const std::vector<ProjectionVariable>& projection_variables,
                const sat::SatParameters& parameters)
      : base_model_(base_model),
        projection_variables_(projection_variables),
        parameters_(parameters),
        values_(projection_variables.size()) {}

  SelectorCpSatResponse Run(size_t max_alternatives) {
    switch (Solve([](sat::CpModelBuilder*) {})) {
      case Outcome::kFeasible:
        break;
      case Outcome::kInfeasible: {
        SelectorCpSatResponse response;
        response.set_status(SOLVER_STATUS_UNSATISFIABLE);
        response.set_assignment_coverage(
            ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
        response.set_solver_response_stats(
            sat::CpSolverResponseStats(last_response_));
        return response;
      }
      case Outcome::kStopped:
        return *stopped_;
    }
    const ProjectionRow first_row = *rows_.begin();

    // A variable is settled while every row found so far agrees with the first
    // row on it. Each feasible round evicts at least one settled variable; an
    // infeasible round proves every remaining settled variable fixed.
    for (;;) {
      std::vector<sat::IntVar> settled;
      std::vector<int64_t> first_values;
      for (size_t i = 0; i < projection_variables_.size(); ++i) {
        if (values_[i].size() == 1) {
          settled.push_back(projection_variables_[i].variable);
          first_values.push_back(first_row.values[i].second);
        }
      }
      if (settled.empty()) {
        break;
      }
      const Outcome outcome = Solve([&](sat::CpModelBuilder* model) {
        model->AddForbiddenAssignments(settled).AddTuple(first_values);
      });
      if (outcome == Outcome::kInfeasible) {
        break;
      }
      if (outcome == Outcome::kStopped) {
        return *stopped_;
      }
    }

    bool capped = false;
    for (size_t i = 0; i < projection_variables_.size(); ++i) {
      if (values_[i].size() == 1) {
        continue;
      }
      const sat::IntVar variable = projection_variables_[i].variable;
      const size_t domain_size = static_cast<size_t>(variable.Domain().Size());
      // Rows found for other variables may also add values to this one.
      bool exhausted = values_[i].size() == domain_size;
      while (!exhausted && values_[i].size() < max_alternatives) {
        const Outcome outcome = Solve([&](sat::CpModelBuilder* model) {
          sat::TableConstraint forbidden =
              model->AddForbiddenAssignments(std::vector<sat::IntVar>{variable});
          for (const int64_t value : values_[i]) {
            forbidden.AddTuple({value});
          }
        });
        if (outcome == Outcome::kStopped) {
          return *stopped_;
        }
        exhausted = outcome == Outcome::kInfeasible ||
                    values_[i].size() == domain_size;
      }
      capped = capped || !exhausted;
    }

    SelectorCpSatResponse response;
    response.set_solver_response_stats(
        sat::CpSolverResponseStats(last_response_));
    response.set_status(rows_.size() == 1 ? SOLVER_STATUS_SATISFIABLE
                                          : SOLVER_STATUS_AMBIGUOUS);
    response.set_assignment_coverage(
        capped ? ASSIGNMENT_COVERAGE_TARGET_SUPPORT_CAPPED
               : ASSIGNMENT_COVERAGE_TARGET_SUPPORT_COMPLETE);
    AddRows(rows_, &response);
    return response;
  }

 private:
  enum class Outcome { kFeasible, kInfeasible, kStopped };

  // Solves the base model plus the constraints `add_constraints` adds to a copy of
  // it. A feasible solve records its projected row; kStopped leaves the
  // response to return in `stopped_`.
  template <typename AddConstraints>
  Outcome Solve(const AddConstraints& add_constraints) {
    sat::CpModelBuilder model = base_model_;
    add_constraints(&model);
    sat::Model solver_model;
    solver_model.Add(sat::NewSatParameters(parameters_));
    const sat::CpModelProto& model_proto = model.Build();
    last_response_ = sat::SolveCpModel(model_proto, &solver_model);
    switch (last_response_.status()) {
      case sat::CpSolverStatus::OPTIMAL:
      case sat::CpSolverStatus::FEASIBLE: {
        ProjectionRow row =
            ProjectionRowFromSolution(last_response_, projection_variables_);
        for (size_t i = 0; i < row.values.size(); ++i) {
          values_[i].insert(row.values[i].second);
        }
        rows_.insert(std::move(row));
        return Outcome::kFeasible;
      }
      case sat::CpSolverStatus::INFEASIBLE:
        return Outcome::kInfeasible;
      case sat::CpSolverStatus::MODEL_INVALID:
        stopped_ = InvalidModelResponse(last_response_, model_proto);
        return Outcome::kStopped;
      case sat::CpSolverStatus::UNKNOWN:
      default:
        stopped_ = UnknownResponse();
        return Outcome::kStopped;
    }
  }

  SelectorCpSatResponse UnknownResponse() const {
    SelectorCpSatResponse response;
    response.set_status(SOLVER_STATUS_UNKNOWN);
    response.set_assignment_coverage(ASSIGNMENT_COVERAGE_SAMPLE);
    response.set_solver_response_stats(
        sat::CpSolverResponseStats(last_response_));
    response.set_diagnostic(
        rows_.empty() ? "CP-SAT returned UNKNOWN"
                      : "CP-SAT stopped before proving complete target support");
    AddRows(rows_, &response);
    return response;
  }

  const sat::CpModelBuilder& base_model_;
  const std::vector<ProjectionVariable>& projection_variables_;
  const sat::SatParameters& parameters_;
  // Rows found so far, deduplicated.
  std::set<ProjectionRow> rows_;
  // Distinct values of projection_variables_[i] across rows_.
  std::vector<std::set<int64_t>> values_;
  sat::CpSolverResponse last_response_;
  std::optional<SelectorCpSatResponse> stopped_;
};

absl::Status BuildCpModel(const SelectorCpSatRequest& request,
                          sat::CpModelBuilder* model,
                          VariableMap* variables) {
  if (const absl::Status status = AddVariables(request, model, variables);
      !status.ok()) {
    return status;
  }
  if (const absl::Status status = AddAllowedTables(request, *variables, model);
      !status.ok()) {
    return status;
  }
  if (const absl::Status status =
          AddBinaryConstraints(request, *variables, model);
      !status.ok()) {
    return status;
  }
  if (const absl::Status status =
          AddLinearConstraints(request, *variables, model);
      !status.ok()) {
    return status;
  }
  if (const absl::Status status =
          AddAllDifferentConstraints(request, *variables, model);
      !status.ok()) {
    return status;
  }
  return absl::OkStatus();
}

}  // namespace

SelectorCpSatResponse SolveSelectorCpSat(const SelectorCpSatRequest& request) {
  if (request.max_alternatives_per_variable() == 0) {
    return InvalidResponse(absl::InvalidArgumentError(
        "max_alternatives_per_variable must be positive"));
  }
  sat::CpModelBuilder model;
  VariableMap variables;
  const absl::Status build_status =
      BuildCpModel(request, &model, &variables);
  if (!build_status.ok()) {
    return InvalidResponse(build_status);
  }

  absl::StatusOr<std::vector<ProjectionVariable>> projection_variables =
      ProjectionVariables(request, variables);
  if (!projection_variables.ok()) {
    return InvalidResponse(projection_variables.status());
  }
  absl::StatusOr<sat::SatParameters> parameters = BuildSatParameters();
  if (!parameters.ok()) {
    return InvalidResponse(parameters.status());
  }

  return SupportSearch(model, *projection_variables, *parameters)
      .Run(request.max_alternatives_per_variable());
}

}  // namespace ducktape::debundle::solver_backends::ortools_cpsat
