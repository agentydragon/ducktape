use common_proto::schema_codegen::v1::{CommonMetadata, SampleStatus};

pub fn generated_metadata() -> CommonMetadata {
    CommonMetadata {
        producer: "rust".to_owned(),
    }
}

pub fn ready_status_number() -> i32 {
    SampleStatus::Ready as i32
}

#[cfg(test)]
mod tests {
    use super::{generated_metadata, ready_status_number};

    #[test]
    fn generated_rust_proto_types_are_usable() {
        assert_eq!(generated_metadata().producer, "rust");
        assert_eq!(ready_status_number(), 1);
    }
}
